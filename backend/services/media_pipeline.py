import copy
import os
import json
import re
import unicodedata
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from services.atomic_write import write_json_atomic

import requests
from PIL import Image, UnidentifiedImageError



def download_image(image_url, output_path):
    # Bounded retry with backoff + Retry-After: a single Wikimedia 429
    # must not kill an otherwise verified image download outright.
    # Phase 3a / C-12: the retry budget is the taxonomy policy —
    # at most ONE transient retry (max_attempts=2) at a 10s
    # per-request timeout; deterministic answers (404/410/rights)
    # are never retried by request_with_retry's status list and
    # raise immediately via raise_for_status.
    response = request_with_retry(
        "GET",
        image_url,
        timeout=IMAGE_HTTP_TIMEOUT,
        max_attempts=1 + download_taxonomy.MAX_TRANSIENT_RETRIES,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    Path(output_path).write_bytes(response.content)

from datetime import datetime, timezone

from services.visual_registry import get_registry
from services import run_metrics
from services import rejection_diagnostics
from services import story_understanding as story
from services import tier_ladder
from services import url_hygiene
from services import provider_health
from services import visual_identity
from services import frame_verification
from services.media_probe import probe_image, probe_video
from services.http_retry import request_with_retry, classify_error
from services import download_taxonomy

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

try:
    from services import process_utils
except ImportError:  # flat-layout fallback
    import process_utils

try:
    from services import segment_index
except ImportError:  # flat-layout fallback
    import segment_index

try:
    from services import timeline as timeline_service
except ImportError:  # flat-layout fallback
    import timeline as timeline_service


# ==========================================================================
# EVIDENCE CLASSIFICATION
# ==========================================================================
# DM-18 DISPOSITION (Quality Recovery Phase 1, recorded — not deleted):
# the evidence-scoring subsystem in this section
# (classify_evidence_type / calculate_visual_evidence_score /
# rank_visuals_by_evidence and their helpers, media_pipeline.py:80-308
# at the audited HEAD) is NOT called by the selection path — live
# selection is verify_candidate + universal_score + the bounded
# format-intent bonus, and no score from this subsystem influences any
# acceptance, ranking or timeline decision. It is quarantined IN PLACE
# as formally classified dead code pending the founder-gated IA-18
# deletion decision; it must not be wired into selection silently
# (that would be a behaviour change requiring its own tests and
# approval), and test_degraded_* pins that the selection path does
# not call it. The fabricated-default half of DM-18 (timeline
# selection_score defaults) is a reporting concern for Phase 3, not
# touched here.

EVIDENCE_IDENTITY = "identity"
EVIDENCE_LOCATION = "location"
EVIDENCE_ORGANIZATION = "organization"
EVIDENCE_OBJECT = "object"
EVIDENCE_EVENT = "event"
EVIDENCE_ACTION = "action"
EVIDENCE_ARCHIVAL = "archival"
EVIDENCE_DOCUMENT = "document"
EVIDENCE_DATA = "data"
EVIDENCE_GENERAL_BROLL = "general_b_roll"

EVENT_SYNONYMS = {
    "bankruptcy": {"bankruptcy", "bankrupt", "insolvency", "insolvent"},
    "landing": {"landing", "landed", "landings"},
    "invasion": {"invasion", "invaded", "invading"},
    "war": {"war", "warfare", "battle"},
    "investigation": {"investigation", "investigated", "investigators"},
    "disappearance": {"disappearance", "disappeared", "missing"},
    "construction": {"construction", "constructed", "building"},
    "invention": {"invention", "invented", "inventor"},
    "launch": {"launch", "launched"},
    "collapse": {"collapse", "collapsed"},
    "crash": {"crash", "crashed"},
    "death": {"death", "died", "killed", "murder"},
    "filing": {"filing", "filed"},
    "attack": {"attack", "attacked"},
    "discovery": {"discovery", "discovered"},
}

WEAK_OVERLAP_TOKENS = {
    "financial", "finance", "money", "market", "stock",
    "earnings", "rally", "investment", "business", "company",
}


def classify_evidence_type(*, asset=None, shot_type="", query="", sentence=""):
    asset = asset or {}
    role = str(asset.get("asset_role", "")).lower().strip()
    scope = str(asset.get("asset_scope", "")).lower().strip()
    shot = str(shot_type or "").lower().strip()
    combined = " ".join([str(query or ""), str(sentence or ""), role, scope, shot]).lower()

    if (asset.get("identity_locked", False) or role == "real_person_photo"
            or scope == "real_person_photo" or shot == "real_person_photo"):
        return EVIDENCE_IDENTITY
    if scope in {"location", "location_photo", "location_broll"} or shot in {"location", "location_photo", "location_broll"}:
        return EVIDENCE_LOCATION
    if shot in {"organization", "company", "corporate"}:
        return EVIDENCE_ORGANIZATION
    if shot in {"document", "legal_document", "official_record"}:
        return EVIDENCE_DOCUMENT
    if shot in {"archival", "historical", "archive"}:
        return EVIDENCE_ARCHIVAL
    if shot in {"event", "event_footage", "incident"}:
        return EVIDENCE_EVENT
    if shot in {"action", "action_broll"}:
        return EVIDENCE_ACTION
    if shot in {"object", "product", "device"}:
        return EVIDENCE_OBJECT
    if shot in {"data", "chart", "graph", "statistics"}:
        return EVIDENCE_DATA
    if any(x in combined for x in ("headquarters", "hq", "factory", "building", "city", "town", "street", "lake", "river", "ocean", "sea", "port", "harbor", "harbour", "beach", "beaches", "courthouse", "campus")):
        return EVIDENCE_LOCATION
    if any(x in combined for x in ("company", "corporation", "organization", "organisation", "brand", "business", "enterprise", "firm")):
        return EVIDENCE_ORGANIZATION
    if any(x in combined for x in ("filing", "court", "lawsuit", "bankruptcy", "report", "record", "statement", "contract", "patent", "letter", "memo", "transcript")):
        return EVIDENCE_DOCUMENT
    if any(x in combined for x in ("archive", "archival", "historical", "historic", "vintage", "newsreel", "old footage", "old photograph")):
        return EVIDENCE_ARCHIVAL
    if any(x in combined for x in ("battle", "war", "collapse", "crash", "attack", "launch", "trial", "murder", "disappearance", "acquisition", "merger", "invention", "incident", "landing", "invasion")):
        return EVIDENCE_EVENT
    if any(x in combined for x in ("building", "manufacturing", "fighting", "sailing", "flying", "running", "walking", "speaking", "filming", "testing", "launching", "working")):
        return EVIDENCE_ACTION
    if any(x in combined for x in ("camera", "ship", "aircraft", "vehicle", "machine", "weapon", "computer", "phone", "prototype", "invention", "device", "product")):
        return EVIDENCE_OBJECT
    if any(x in combined for x in ("data", "chart", "graph", "statistics", "statistic", "percentage", "revenue", "price", "market", "population", "counter")):
        return EVIDENCE_DATA
    return EVIDENCE_GENERAL_BROLL


def calculate_evidence_strength(*, asset=None, evidence_type="", sentence=""):
    asset = asset or {}
    if asset.get("identity_locked", False):
        return "high"
    role = str(asset.get("asset_role", "")).lower()
    source = str(asset.get("source", "")).lower()
    if evidence_type == EVIDENCE_IDENTITY:
        return "high"
    if role in {"real_person_photo", "archival", "historical", "official_document"}:
        return "high"
    if evidence_type in {EVIDENCE_LOCATION, EVIDENCE_ORGANIZATION, EVIDENCE_OBJECT, EVIDENCE_EVENT, EVIDENCE_DOCUMENT, EVIDENCE_ARCHIVAL}:
        return "medium"
    if evidence_type in {EVIDENCE_ACTION, EVIDENCE_DATA}:
        return "medium"
    return "low"


def _analyze_current_text(text):
    try:
        from services.script_analyzer import analyze_sentence_fast
        return analyze_sentence_fast(text) or {}
    except Exception:
        return {}


def extract_evidence_entity(*, evidence_type="", sentence="", query="", asset=None):
    asset = asset or {}
    evidence_type = str(evidence_type or "").lower().strip()
    sentence_text = str(sentence or "").strip()
    query_text = str(query or "").strip()
    analysis = _analyze_current_text(sentence_text)
    entities = list(analysis.get("entities") or [])
    locations = list(analysis.get("locations") or [])
    objects = list(analysis.get("objects") or [])
    dates = list(analysis.get("dates") or [])
    event = str(analysis.get("event") or "").strip()

    if asset.get("identity_locked", False):
        identity = asset.get("identity_entity") or asset.get("person_name") or asset.get("entity_name") or ""
        if str(identity).strip():
            return str(identity).strip()
    if evidence_type == EVIDENCE_IDENTITY:
        return str(asset.get("person_name") or asset.get("identity_entity") or "").strip() or (entities[0] if entities else "")
    if evidence_type == EVIDENCE_ORGANIZATION:
        return str(asset.get("entity_name") or asset.get("organization") or asset.get("company") or "").strip() or (entities[0] if entities else "")
    if evidence_type == EVIDENCE_LOCATION:
        return str(asset.get("location") or asset.get("entity_name") or "").strip() or (locations[0] if locations else (entities[0] if entities else ""))
    if evidence_type == EVIDENCE_OBJECT:
        return str(asset.get("entity_name") or asset.get("object_name") or "").strip() or (objects[0] if objects else "")
    if evidence_type == EVIDENCE_DOCUMENT:
        combined = " ".join([sentence_text, query_text]).lower()
        for document in ("bankruptcy filing", "court filing", "court document", "sec filing", "patent", "lawsuit", "contract", "report", "transcript", "statement", "memo", "letter", "filing"):
            if document in combined:
                return document
        return str(asset.get("document_name") or asset.get("entity_name") or "").strip()
    if evidence_type == EVIDENCE_DATA:
        match = re.search(
            r"\$\s?\d+(?:\.\d+)?\s?(?:million|billion|trillion)?"
            r"|\b\d+(?:\.\d+)?\s?(?:kg|lbs?|million|billion|trillion)\b"
            r"|\b\d+(?:\.\d+)?%",
            " ".join([sentence_text, query_text]), flags=re.I)
        return match.group(0) if match else str(asset.get("data_entity") or asset.get("entity_name") or "").strip()
    if evidence_type == EVIDENCE_EVENT:
        return str(asset.get("event_name") or asset.get("entity_name") or "").strip() or event
    if evidence_type == EVIDENCE_ARCHIVAL:
        if dates:
            return dates[0]
        year_match = re.search(r"\b(?:18|19|20)\d{2}\b", " ".join([sentence_text, query_text]))
        return year_match.group(0) if year_match else str(asset.get("entity_name") or "").strip()
    return str(asset.get("entity_name") or asset.get("evidence_entity") or "").strip()


def enrich_visual_with_evidence(visual, *, shot_type="", query="", sentence=""):
    if not isinstance(visual, dict):
        return visual
    evidence_type = classify_evidence_type(asset=visual, shot_type=shot_type, query=query, sentence=sentence)
    visual["evidence_type"] = evidence_type
    visual["evidence_strength"] = calculate_evidence_strength(asset=visual, evidence_type=evidence_type, sentence=sentence)
    visual["evidence_entity"] = extract_evidence_entity(evidence_type=evidence_type, sentence=sentence, query=query, asset=visual)
    reasons = {
        EVIDENCE_IDENTITY: "identity_locked_real_person", EVIDENCE_LOCATION: "location_context",
        EVIDENCE_ORGANIZATION: "organization_context", EVIDENCE_OBJECT: "object_or_product_context",
        EVIDENCE_EVENT: "event_context", EVIDENCE_ACTION: "action_context",
        EVIDENCE_ARCHIVAL: "historical_archival_context", EVIDENCE_DOCUMENT: "document_or_record_context",
        EVIDENCE_DATA: "data_or_statistical_context", EVIDENCE_GENERAL_BROLL: "general_b_roll",
    }
    visual["evidence_reason"] = reasons.get(evidence_type, "general_b_roll")
    return visual


def infer_preferred_evidence_types(sentence=""):
    sentence_text = str(sentence or "").lower().strip()
    if not sentence_text:
        return [EVIDENCE_GENERAL_BROLL]
    preferred = []

    def add(*types):
        for evidence_type in types:
            if evidence_type not in preferred:
                preferred.append(evidence_type)

    if any(p in sentence_text for p in ("person", "founder", "inventor", "ceo", "executive", "president", "director")):
        add(EVIDENCE_IDENTITY)
    if any(p in sentence_text for p in ("company", "corporation", "organization", "organisation", "brand", "business", "enterprise", "firm", "manufacturer", "employer", "headquarters")):
        add(EVIDENCE_ORGANIZATION)
    if any(p in sentence_text for p in ("city", "town", "village", "country", "state", "located", "location", "headquarters", "factory", "plant", "campus", "street", "lake", "river", "ocean", "sea", "port", "harbor", "harbour", "beach", "beaches", "courthouse", "battlefield")):
        add(EVIDENCE_LOCATION)
    if any(p in sentence_text for p in ("camera", "prototype", "device", "machine", "computer", "phone", "ship", "aircraft", "vehicle", "product", "invention", "technology", "equipment")):
        add(EVIDENCE_OBJECT)
    if any(p in sentence_text for p in ("document", "documents", "filing", "filed", "court", "lawsuit", "legal", "contract", "patent", "report", "record", "records", "statement", "letter", "memo", "transcript")):
        add(EVIDENCE_DOCUMENT)
    if any(p in sentence_text for p in ("invention", "invented", "launched", "launch", "acquired", "acquisition", "merger", "merged", "bankruptcy", "collapsed", "collapse", "crash", "attack", "battle", "war", "trial", "murder", "disappearance", "disappeared", "incident", "accident", "death", "died", "founded", "landed", "landing", "invaded", "invasion")):
        add(EVIDENCE_EVENT)
    if any(p in sentence_text for p in ("built", "created", "developed", "designed", "manufactured", "produced", "tested", "testing", "working", "worked", "searched", "searching", "investigated", "investigating", "filmed", "speaking", "walking", "running", "flying", "sailing", "fighting")):
        add(EVIDENCE_ACTION)
    if re.search(r"\b(18|19|20)\d{2}\b", sentence_text) or any(p in sentence_text for p in ("historical", "history", "historic", "archive", "archival", "vintage", "old footage", "old photograph", "years earlier", "decades earlier", "at the time", "originally")):
        add(EVIDENCE_ARCHIVAL)
    if (re.search(r"\b\d+(?:\.\d+)?%\b", sentence_text) or re.search(r"\$\s?\d", sentence_text)
            or re.search(r"\b\d+(?:\.\d+)?\s?(?:kg|lbs?|million|billion|trillion)\b", sentence_text)
            or any(p in sentence_text for p in ("revenue", "profit", "loss", "sales", "stock", "share price", "market", "percentage", "percent", "statistics", "data", "million", "billion", "trillion", "declined", "increased", "grew", "fell", "rose"))):
        add(EVIDENCE_DATA)
    if not preferred:
        add(EVIDENCE_GENERAL_BROLL)
    return preferred


EVIDENCE_STRENGTH_SCORES = {"high": 100, "medium": 65, "low": 25}


def calculate_visual_evidence_score(visual, *, sentence="", preferred_evidence_types=None):
    if not isinstance(visual, dict):
        return 0
    preferred = {str(v).lower().strip() for v in (preferred_evidence_types or []) if str(v).strip()}
    evidence_type = str(visual.get("evidence_type", "")).lower().strip()
    strength = str(visual.get("evidence_strength", "")).lower().strip()
    score = EVIDENCE_STRENGTH_SCORES.get(strength, 0)
    if visual.get("identity_locked", False):
        score += 100
    if evidence_type in preferred:
        score += 60
    sentence_text = str(sentence or "").lower().strip()
    entity = str(visual.get("evidence_entity", "")).lower().strip()
    if entity and entity in sentence_text:
        score += 50
    if str(visual.get("source", "")).lower().strip() == "youtube":
        score += 10
    if evidence_type == EVIDENCE_GENERAL_BROLL:
        score -= 20
    return max(0, int(score))


def rank_visuals_by_evidence(visuals, *, sentence="", preferred_evidence_types=None):
    if not isinstance(visuals, list):
        return []
    ranked = []
    for index, visual in enumerate(visuals):
        if not isinstance(visual, dict):
            continue
        score = calculate_visual_evidence_score(visual, sentence=sentence, preferred_evidence_types=preferred_evidence_types)
        visual["evidence_score"] = score
        ranked.append((bool(visual.get("identity_locked", False)), score, index, visual))
    ranked.sort(key=lambda item: (item[0], item[1], -item[2]), reverse=True)
    return [item[3] for item in ranked]


def enrich_visual_collection_with_evidence(visuals, *, shot_type="", query="", sentence=""):
    if not isinstance(visuals, list):
        return visuals
    for visual in visuals:
        enrich_visual_with_evidence(visual, shot_type=shot_type, query=query, sentence=sentence)
    return visuals


# ==========================================================================
# CONFIG / HELPERS
# ==========================================================================

DEFAULT_IMAGE_COUNT = 3
DEFAULT_VIDEO_COUNT = 2
DEFAULT_CLIP_DURATION = 8

# Segment-retrieval provenance (founder directive: download only the
# segment required, and prove it). The real download functions record
# HOW each clip was retrieved here, keyed by output path; the asset
# builder pops the entry and stamps it onto the accepted asset.
# Methods: "yt-dlp-download-sections" (partial), "ffmpeg-range-seek"
# (partial), "full-download-trim" (NOT partial — reported honestly).
DOWNLOAD_PROVENANCE: dict = {}

# Source durations learned during segment-evidence checks, keyed by
# URL, so accepted assets can report original vs extracted duration
# without re-probing remote sources.
_SOURCE_DURATION_CACHE: dict = {}

# VA-03/VA-06: per-run segment-evidence bookkeeping, keyed by URL.
# _SEGMENT_BASIS records HOW a start time was established inside
# _youtube_start ("chapter" | "transcript" | "description" |
# "positional_no_contract") so a positional estimate is never
# relabelled as verified evidence downstream. _SEGMENT_ATTEMPTS lists
# every evidence source tried for a URL, in order, with its outcome.
# _SEGMENT_FAILURE_CAUSE records why evidence failed, distinguishing
# a broken information channel from genuinely absent evidence.
_SEGMENT_BASIS: dict = {}
_SEGMENT_ATTEMPTS: dict = {}
_SEGMENT_FAILURE_CAUSE: dict = {}
# Stage 7 / R8: extracted-clip cache keyed by selection segment_id
# (see _uve_download_video_candidate). Per-run via RunContext.
_SEGMENT_FILE_CACHE: dict = {}

# yt-dlp TLS trust (founder investigation, 2026-11-19): yt-dlp verifies
# TLS against certifi's bundled Mozilla roots by default and ignores
# both the system trust store and SSL_CERT_FILE. Behind a TLS-
# intercepting egress proxy whose CA is installed in the system store
# (and named by SSL_CERT_FILE / REQUESTS_CA_BUNDLE), every extraction
# therefore fails with CERTIFICATE_VERIFY_FAILED even though curl,
# git and requests — which honour the environment bundle — work.
# When the environment configures a CA bundle, pass yt-dlp's own
# supported --compat-options no-certifi so it verifies against the
# system / environment trust chain instead. Verification stays ON;
# --no-check-certificate is never used anywhere in this pipeline.
def _yt_dlp_base_command() -> list:
    command = [sys.executable, "-m", "yt_dlp"]
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"):
        command += ["--compat-options", "no-certifi"]
    return command

# Phase 5.4 split: the discovery seam (providers, hit ranking, title
# filters, dedupe drops, _uve_scenario_search) lives in
# services.pipeline_discovery and is re-exported here so every
# existing caller and test reference keeps working unchanged. The
# per-run discovery-failure / circuit-breaker state (_DISCOVERY_FAILURES,
# _search_failure_state, _record_discovery_failure,
# drain_discovery_failures, SearchCircuitBreakerTripped) deliberately
# STAYS in this module: those containers are rebound by the single
# RunContext binder registered below, and the breaker cluster owns
# them — see the Phase 5.4 note at _record_discovery_failure.
try:
    from services.pipeline_discovery import (
        _DEDUPE_DROPS,
        _KNOWN_VIDEO_PROVIDERS,
        _QUERY_LOG_STACK,
        ANIMAL_WORDS,
        FACE_WORDS,
        _rank_hits,
        _record_dedupe_drop,
        _slug_title,
        _uve_scenario_search,
        _web_image_hit,
        _web_source_is_video_candidate,
        _youtube_video_hit,
        blocked_visual,
        drain_dedupe_drops,
        is_direct_media_url,
        looks_like_animal,
        looks_like_face,
        mentions_named_person,
        provider_for_url,
        query_relevance,
        search_archive_org_videos,
        search_image_sources,
        search_pexels_images,
        search_pexels_videos,
        search_pixabay_images,
        search_pixabay_videos,
        search_videos_multi,
        search_web_sources,
        search_wikimedia_images,
        search_wikimedia_videos,
        search_youtube_videos,
        search_source_videos,
        _disabled_providers,
    )
except ImportError:  # flat-layout fallback
    from pipeline_discovery import (
        _DEDUPE_DROPS,
        _KNOWN_VIDEO_PROVIDERS,
        _QUERY_LOG_STACK,
        ANIMAL_WORDS,
        FACE_WORDS,
        _rank_hits,
        _record_dedupe_drop,
        _slug_title,
        _uve_scenario_search,
        _web_image_hit,
        _web_source_is_video_candidate,
        _youtube_video_hit,
        blocked_visual,
        drain_dedupe_drops,
        is_direct_media_url,
        looks_like_animal,
        looks_like_face,
        mentions_named_person,
        provider_for_url,
        query_relevance,
        search_archive_org_videos,
        search_image_sources,
        search_pexels_images,
        search_pexels_videos,
        search_pixabay_images,
        search_pixabay_videos,
        search_videos_multi,
        search_web_sources,
        search_wikimedia_images,
        search_wikimedia_videos,
        search_youtube_videos,
        search_source_videos,
        _disabled_providers,
    )

# Phase 5.4 split: the verification hand-off seam (candidate identity,
# URL hygiene, _uve_verify_all, format classification, frame-check
# hand-off, content hash, _uve_fetch_first) lives in
# services.pipeline_verification and is re-exported here. The download
# machinery (_uve_download_any_candidate and the two candidate
# downloaders) and the transcript-check chain deliberately STAY in
# this module — see the coupling note in pipeline_verification.
try:
    from services.pipeline_verification import (
        _CONTENT_HASH_CACHE,
        _VISUAL_FORMAT_TERMS,
        _apply_frame_check,
        _apply_url_hygiene,
        _candidate_id_for,
        _uve_annotate,
        _uve_candidate_key,
        _uve_content_hash,
        _uve_fetch_first,
        _uve_verify_all,
        classify_visual_format,
    )
except ImportError:  # flat-layout fallback
    from pipeline_verification import (
        _CONTENT_HASH_CACHE,
        _VISUAL_FORMAT_TERMS,
        _apply_frame_check,
        _apply_url_hygiene,
        _candidate_id_for,
        _uve_annotate,
        _uve_candidate_key,
        _uve_content_hash,
        _uve_fetch_first,
        _uve_verify_all,
        classify_visual_format,
    )


# Phase 5.4 split: the assignment/coverage seam (eligibility gate +
# Phase 5 matching helpers, intent-aware pick, coverage helpers) lives
# in services.pipeline_assignment and is re-exported here. The
# coverage orchestration (_uve_search_and_pick with its nested draw
# loop and tier-escalation hand-off, _uve_process_scene_media)
# deliberately STAYS in this module — see the coupling note in
# pipeline_assignment.
try:
    from services.pipeline_assignment import (
        COVERAGE_DRAW_MAX_ATTEMPTS,
        INTENT_FIT_MAX,
        METADATA_ABSENCE_PENALTY,
        MISREPRESENTATION_DEMOTION,
        REUSE_PENALTY_PER_USE,
        STOP_BEAT_DOWNLOAD_ATTEMPT_CAP,
        VIDEO_PREFERENCE_EPSILON,
        _INTENT_IMAGE_INTENTS,
        _INTENT_VIDEO_INTENTS,
        _NAME_STOPWORDS,
        _TIEBREAK_EPSILON,
        _TYPE_TOKEN_STOPWORDS,
        _alias_match,
        _apply_source_quality_tiebreak,
        _apply_video_preference,
        _beat_wants_video,
        _fold_name,
        _gate_type_tokens,
        _intent_rank_adjustment,
        _name_match,
        _name_tokens,
        _place_match,
        _place_satisfied,
        _sig_name_tokens,
        _subject_match,
        _title_blob,
        _uve_beat_distinct_asset_keys,
        _uve_beat_distinct_asset_level_keys,
        _uve_coverage_shortfall,
        _uve_pick,
        _uve_prepare_sentence_result,
        _uve_selection_block_reason,
        gate_all_failures,
        gate_shot_need,
    )
except ImportError:  # flat-layout fallback
    from pipeline_assignment import (
        COVERAGE_DRAW_MAX_ATTEMPTS,
        INTENT_FIT_MAX,
        METADATA_ABSENCE_PENALTY,
        MISREPRESENTATION_DEMOTION,
        REUSE_PENALTY_PER_USE,
        STOP_BEAT_DOWNLOAD_ATTEMPT_CAP,
        VIDEO_PREFERENCE_EPSILON,
        _INTENT_IMAGE_INTENTS,
        _INTENT_VIDEO_INTENTS,
        _NAME_STOPWORDS,
        _TIEBREAK_EPSILON,
        _TYPE_TOKEN_STOPWORDS,
        _alias_match,
        _apply_source_quality_tiebreak,
        _apply_video_preference,
        _beat_wants_video,
        _fold_name,
        _gate_type_tokens,
        _intent_rank_adjustment,
        _name_match,
        _name_tokens,
        _place_match,
        _place_satisfied,
        _sig_name_tokens,
        _subject_match,
        _title_blob,
        _uve_beat_distinct_asset_keys,
        _uve_beat_distinct_asset_level_keys,
        _uve_coverage_shortfall,
        _uve_pick,
        _uve_prepare_sentence_result,
        _uve_selection_block_reason,
        gate_all_failures,
        gate_shot_need,
    )

# Per-run resilience state (cleared by reset_media_caches each job):
# URLs whose download already failed in this run are never re-requested
# by a later beat/scene — a throttled or dead URL stays dead for the run
# instead of burning discovery budget again and again.
_FAILED_DOWNLOAD_URLS: set = set()

# Phase 3 / AM-016: the failed-download guard's segment keys live in
# the same set, prefixed "seg:" (see _uve_download_video_candidate /
# _uve_fetch_first).

# Gate 4 media seams (2026-11-26; diagnosis 08 §6–§7): per-run
# download memory at the dispatcher seam (_uve_download_any_candidate),
# keyed by the canonical identity of the ACTUAL download URL. The
# _FAILED_DOWNLOAD_URLS guard above keys on the candidate's page /
# source identity, which misses two evidenced holes: the same image
# FILE offered under different source pages (its download URL is the
# candidate's image_url, not its url), and a video re-presented at a
# different segment after a terminal failure. These containers close
# both, and add the cross-beat content block: a still's downloaded
# bytes, once committed to one beat, are never committed to another.
#   _DOWNLOAD_URL_FAILURES: canonical download-URL key ->
#       {"retryable": bool, "failure_class": ..., "reason": ...}
#       (the download taxonomy's own classification of the attempt)
#   _DOWNLOADED_ASSETS_BY_URL: canonical download-URL key -> the
#       asset template of a succeeded IMAGE download (videos resolve
#       re-presentations through _SEGMENT_FILE_CACHE instead)
#   _COMMITTED_CONTENT_OWNERS: still content hash -> the sentence
#       number that first committed those bytes this run
_DOWNLOAD_URL_FAILURES: dict = {}
_DOWNLOADED_ASSETS_BY_URL: dict = {}
_COMMITTED_CONTENT_OWNERS: dict = {}

# Phase 2 (Discovery Efficiency): the DURABLE discovery memory —
# services.discovery_memory, one SQLite store per project directory.
# The per-run containers above are now the run's working set, seeded
# from this store when a production's media processing opens it and
# written through on every new failure, so attempted URLs, rejections
# and download failures survive scenes, runs and process restarts
# (Phase 0 root cause 2: discovery had no memory beyond the run).
# The handle is module state, NOT RunContext state: it is keyed by
# project directory, opened lazily by _open_discovery_memory, and
# every consumer treats None (store unavailable) as "no memory" —
# the store is fail-soft by contract and never blocks a production.
_discovery_memory = None
_discovery_memory_project_dir: str | None = None


def _open_discovery_memory(project_dir):
    """Open (or reuse) the durable discovery memory for a project
    directory and seed this run's failure guards from it.

    Called when a scene's media processing starts (the first moment
    the project directory is known). Seeding rules: durable download
    failures re-enter _DOWNLOAD_URL_FAILURES with their recorded
    taxonomy (retryable flag included — the dispatcher refuses
    terminal failures and the store has already expired transient
    ones past its TTL); durable source/segment failures re-enter
    _FAILED_DOWNLOAD_URLS. Existing in-run entries win — seeding
    never overwrites fresher run state. Returns the store or None."""
    global _discovery_memory, _discovery_memory_project_dir
    try:
        from services import discovery_memory as _dm
    except ImportError:  # flat-layout fallback
        import discovery_memory as _dm
    try:
        store = _dm.DiscoveryMemory.open(project_dir)
    except Exception:
        store = None
    _discovery_memory = store
    _discovery_memory_project_dir = str(project_dir or "") or None
    if store is None:
        return None
    try:
        for scope, key, record in store.iter_download_failures():
            if scope == "download_url":
                _DOWNLOAD_URL_FAILURES.setdefault(key, {
                    "retryable": bool(record.get("retryable")),
                    "failure_class": record.get("failure_class"),
                    "reason": record.get("reason"),
                })
            elif scope == "source":
                _FAILED_DOWNLOAD_URLS.add(key)
            elif scope == "segment":
                # Store keys are bare segment ids; the in-run guard
                # set carries them "seg:"-prefixed (AM-016).
                _FAILED_DOWNLOAD_URLS.add(f"seg:{key}")
    except Exception:
        pass
    return store


def _active_discovery_memory():
    """The durable discovery memory for the ACTIVE production run,
    or None.

    Scoping rule (mirrors pipeline_verification._verdict_cache):
    the durable layer belongs to genuinely activated run contexts —
    jobs activates one per production, and that production's scene
    processing opens its store before any discovery runs, so the
    module handle above always names the current production's store
    by the time anything reads it. The DEFAULT context (unit tests,
    ad-hoc calls) has NO durable memory: direct verification/fetch
    calls evaluate exactly as they did before Phase 2, and one
    test's scene processing can never leak verdicts into another
    test's direct calls through a leftover handle."""
    try:
        ctx = run_context.current_context()
        if ctx is None or ctx is run_context.default_context():
            return None
    except Exception:
        return None
    return _discovery_memory

# Discovery failures recorded by _uve_scenario_search (provider, query,
# error) so the asset report can distinguish DISCOVERY failures from
# download and verification failures instead of losing them in prints.
#
# Phase 5.4 note: this cluster — _DISCOVERY_FAILURES,
# _search_failure_state, _record_discovery_failure,
# drain_discovery_failures, SearchCircuitBreakerTripped and the
# _record_search_* breaker — deliberately did NOT move to
# services.pipeline_discovery with the rest of the discovery seam.
# The two containers are rebound per production run by the single
# RunContext binder registered in this module (run_context supports
# exactly one binder); moving the containers without moving the
# binder would silently split per-run state across two namespaces.
# pipeline_discovery reaches _record_discovery_failure through this
# module's namespace at call time, like the provider entry points.
_DISCOVERY_FAILURES: list = []

def _record_discovery_failure(provider, query, error, *, attempt=1, retry_count=0):
    """Record one discovery (provider search) failure.

    Quality Recovery Phase 2 (workstream D; DM-09): the record carries
    the full failure shape the brief requires — provider, query,
    attempt number, error-class enum, HTTP status where available,
    transient/non-transient classification, retry count and circuit-
    breaker state — so a provider failure is never collapsed with an
    empty result set (zero failures recorded != failures unknown; the
    artefact-level distinction is completed in Phase 3 reporting).
    The legacy "error" string is kept for existing consumers.
    """
    classification = classify_error(error)
    _DISCOVERY_FAILURES.append(
        {
            "provider": str(provider),
            "query": str(query)[:160],
            "error": f"{type(error).__name__}: {error}"[:200],
            "attempt": int(attempt or 1),
            "error_class": classification["error_class"],
            "http_status": classification["http_status"],
            "transient": classification["transient"],
            "retry_count": int(retry_count or 0),
            "breaker_state": {
                "consecutive_failures": int(
                    _search_failure_state.get("consecutive_failures") or 0
                ),
                "tripped": bool(
                    (_search_failure_state.get("consecutive_failures") or 0)
                    >= MAX_CONSECUTIVE_SEARCH_FAILURES
                ),
            },
        }
    )


def drain_discovery_failures():
    """Return and clear the discovery failures recorded since last drain."""
    failures = list(_DISCOVERY_FAILURES)
    _DISCOVERY_FAILURES.clear()
    return failures


MAX_CONSECUTIVE_SEARCH_FAILURES = 8
# ---------------------------------------------------------------------------
# Download time bounds (Phase 3a / C-12). Before this change a doomed
# candidate could burn ~5 minutes: yt-dlp ran under a 180s ceiling
# (with 5 internal retries / 5 fragment retries at a 30s socket
# timeout) followed by a 90s normalisation ffmpeg (270s worst case),
# and the direct path stacked 90s range-seek + 60s HTTP + 90s trim
# (240s). The bounds below are per-candidate AGGREGATE budgets:
# every subprocess/HTTP step in a candidate's download is capped so
# the paths sum to well under the old figure —
#   YouTube path:  YT_DLP_TIMEOUT 60 + NORMALIZE_FFMPEG_TIMEOUT 120
#                  = 180s (Gate 4: normalisation split out — see the
#                  NORMALIZE_FFMPEG_TIMEOUT note below)
#   direct path:   seek 45 + HTTP read 20 + trim 45               = 110s
#   image path:    2 outer attempts x (2 requests x 10s + 1s
#                  backoff) + 0.5s outer delay                    ~ 43s
# Retry policy (services.download_taxonomy): deterministic failures
# (404/410/other 4xx, rights rejections, verification failures) are
# never retried; transient failures get at most one retry.
# ---------------------------------------------------------------------------
YT_DLP_TIMEOUT = 60
YTDLP_RETRIES = 2
YTDLP_FRAGMENT_RETRIES = 2
YTDLP_SOCKET_TIMEOUT = 10
FFMPEG_TIMEOUT = 90
DOWNLOAD_FFMPEG_TIMEOUT = 45
# Gate 4 segment-loss fix (diagnosis 12, F1/F2): the YouTube
# normalisation is a SEPARATE, heavier step than the fetch — the
# blurred-fill filtergraph re-encodes at 1080p — so it gets its own
# ceiling instead of sharing the direct path's trim bound. Run 4
# measured the normalisation dying at the 45s ceiling with only
# ~4.1–4.9s of an 8s clip written at elapsed ≈44s (≈0.10× realtime
# on this VM), i.e. a full 8s clip needs ~73–88s; 120s covers the
# worst measured rate with ~1.4× headroom. The fetch ceiling stays
# 60s: 9 of run 4's 12 timed-out fetches had in fact delivered
# complete valid 8s section files by the kill — the defect was
# discarding them, fixed by the salvage path in download_video_clip
# — so a longer fetch ceiling would only lengthen doomed attempts.
NORMALIZE_FFMPEG_TIMEOUT = 120
#: A timed-out fetch's leftover section file counts as covering the
#: requested window when its probed duration is within this many
#: seconds of the requested duration (run 4's complete leftovers
#: probed 8.000–8.008s against an 8s request; a mid-write stub
#: probes as no video at all, and a genuinely short partial section
#: stays a failure).
SALVAGE_DURATION_TOLERANCE_SECONDS = 0.5
DIRECT_HTTP_TIMEOUT = (5, 20)  # (connect, read) seconds, per read
IMAGE_HTTP_TIMEOUT = 10

#: Last download failure detail per output path (Phase 3a / C-02),
#: the DOWNLOAD_PROVENANCE pattern: the low-level downloaders know
#: WHY a candidate died (HTTP status, timeout, stderr tail) while
#: their callers only see a bool. They record it here; the
#: candidate downloaders pop it and stamp the taxonomy record on
#: the candidate so the failure aggregate can classify it.
DOWNLOAD_LAST_ERROR: dict = {}


def _record_download_error(output_path, reason, *, http_status=None,
                           detail=None, error=None):
    classified = download_taxonomy.classify_download_failure(
        reason, http_status=http_status, detail=detail, error=error,
    )
    DOWNLOAD_LAST_ERROR[str(output_path)] = {
        "reason": str(reason),
        "failure_class": classified["failure_class"],
        "http_status": classified["http_status"],
        "check": classified["check"],
        "retryable": classified["retryable"],
        "detail": str(detail or "")[-500:],
    }
TEMP_FILE_SUFFIXES = (".part", ".ytdl", ".temp")
TEMP_FILE_PATTERNS = ("*.part", "*.ytdl", "*.temp")
MAX_IDENTITY_PEOPLE_PER_SCENE = 8
NO_SEARCH_ROLES = {"transition", "reflection", "hook", "conclusion", "motivation"}
MAX_BEAT_SENTENCES = 5

_search_failure_state = {"consecutive_failures": 0, "last_error": None}
_STOP = {"a", "an", "the", "of", "in", "on", "at", "to", "and", "or", "for", "with", "from", "missing", "news", "photo", "image"}


class SearchCircuitBreakerTripped(RuntimeError):
    pass


def _record_search_success():
    _search_failure_state["consecutive_failures"] = 0


def _record_search_failure(error):
    _search_failure_state["consecutive_failures"] += 1
    _search_failure_state["last_error"] = error
    if _search_failure_state["consecutive_failures"] >= MAX_CONSECUTIVE_SEARCH_FAILURES:
        raise SearchCircuitBreakerTripped(
            f"Search API has failed {_search_failure_state['consecutive_failures']} times. Last error: {error!r}")


BEAT_HINTS = [
    (re.compile(r"\b(police|detective|sheriff|arrest|search(ed|ing)?)\b", re.I), "police"),
    (re.compile(r"\b(court|trial|jury|judge|sentence|convict|appeal|courthouse)\b", re.I), "court"),
    (re.compile(r"\b(news|reporter|journalist|headline|press)\b", re.I), "press"),
    (re.compile(r"\b(lake|boat|fish|water|dock|river|ocean|sea|beach)\b", re.I), "water"),
    (re.compile(r"\b(document|letter|file|record|disk|metadata)\b", re.I), "documents"),
    (re.compile(r"\b(church|neighbor|family|community)\b", re.I), "community"),
]


@dataclass
class EntityBank:
    people: list = field(default_factory=list)
    places: list = field(default_factory=list)
    dates: list = field(default_factory=list)
    objects: list = field(default_factory=list)


@dataclass
class ShotNeed:
    beat_id: str
    shot_index: int
    shot_type: str
    description: str
    queries: list


@dataclass
class Beat:
    beat_id: str
    label: str
    start: int
    end: int
    text: str
    sentence_indexes: list
    shots: list


def _http():
    session = requests.Session()
    session.headers.update({"User-Agent": "DocumentaryStudio/0.4"})
    return session


def uniq(items):
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def extract_entities(text):
    analysis = _analyze_current_text(text)
    return EntityBank(
        people=uniq(list(analysis.get("entities") or [])),
        places=uniq(list(analysis.get("locations") or [])),
        dates=uniq(list(analysis.get("dates") or [])),
        objects=uniq(list(analysis.get("objects") or [])),
    )


def _queries_from_text(text):
    analysis = _analyze_current_text(text)
    queries = list(analysis.get("search_queries") or [])
    entities = list(analysis.get("entities") or [])
    locations = list(analysis.get("locations") or [])
    dates = list(analysis.get("dates") or [])
    if entities:
        queries.append(entities[0])
        queries.append(f"{entities[0]} archival")
    if entities and locations:
        queries.append(f"{entities[0]} {locations[0]}")
    if locations:
        queries.append(locations[0])
    if dates and entities:
        queries.append(f"{entities[0]} {dates[0]}")
    return uniq([q for q in queries if str(q).strip()])[:8]


def detect_label(text):
    for pattern, label in BEAT_HINTS:
        if pattern.search(text or ""):
            return label
    return "general"


def segment_beats(sentences, min_len=3, max_len=8):
    if not sentences:
        return []
    labels = [detect_label(item) for item in sentences]
    groups, start, current = [], 0, labels[0]
    for index, label in enumerate(labels):
        long_enough = (index - start) >= min_len
        too_long = (index - start + 1) > max_len
        if (label != current and long_enough) or too_long:
            groups.append((current, start, index - 1))
            start, current = index, label
    groups.append((current, start, len(sentences) - 1))
    beats = []
    for beat_no, (label, s0, e0) in enumerate(groups, start=1):
        indexes = list(range(s0, e0 + 1))
        text = " ".join(sentences[i] for i in indexes)
        queries = _queries_from_text(text) or [text[:80]]
        shots = [ShotNeed(f"beat_{beat_no:02d}_{label}", n, "search", f"{label} shot {n}",
                          queries[n - 1:n] or queries[:1])
                 for n in range(1, min(4, max(2, len(queries) + 1)))]
        beats.append(Beat(f"beat_{beat_no:02d}_{label}", label, s0, e0, text, indexes, shots))
    return beats


def entity_anchor_shots(entities):
    shots, index = [], 1
    for person in entities.people:
        shots.append(ShotNeed("entity_anchors", index, "real_person_photo", person, [person, f"{person} photo", f"{person} archive"]))
        index += 1
    for place in entities.places:
        shots.append(ShotNeed("entity_anchors", index, "location_photo", place, [place, f"{place} historical", f"{place} archival"]))
        index += 1
    for obj in entities.objects:
        shots.append(ShotNeed("entity_anchors", index, "anchor", obj, [str(obj), f"{obj} archival"]))
        index += 1
    return shots


def generic_filler_shots():
    return []


def build_visual_plan(sentences):
    entities = extract_entities(" ".join(sentences))
    beats = segment_beats(sentences)
    shots = entity_anchor_shots(entities)
    for beat in beats:
        shots.extend(beat.shots)
    return {"entities": entities, "beats": beats, "shots": shots}


# ==========================================================================
# CASE BIBLE
# ==========================================================================

def _load_case_bible(scene, project_dir):
    raw_bible = None
    if isinstance(scene, dict):
        raw_bible = scene.get("bible") or scene.get("story_bible")
        bible_path = scene.get("case_bible_path") or ""
    else:
        bible_path = ""
    if not raw_bible:
        if not bible_path:
            bible_path = os.path.join(project_dir, "case_bible.json")
        if bible_path and os.path.exists(bible_path):
            try:
                with open(bible_path, "r", encoding="utf-8") as handle:
                    raw_bible = json.load(handle)
            except Exception as exc:
                print(f"CASE BIBLE FILE READ FAILED: {exc}")
                raw_bible = None
    try:
        bible = story.hydrate_bible(raw_bible)
    except Exception as exc:
        print(f"CASE BIBLE LOAD SKIPPED: {exc}")
        bible = story.hydrate_bible({})
    print(
        "CASE BIBLE LOADED: "
        f"{bible.get('title') or '(untitled)'} "
        f"entities={len(bible.get('entities') or [])} "
        f"era={bible.get('story_era') or bible.get('time_period')}"
    )
    return bible


# ==========================================================================
# SENTENCE ACCESS
# ==========================================================================

def get_sentence_list(scene):
    sentences = scene.get("sentences") or scene.get("sentence_analysis") or scene.get("sentence_data") or []
    return sentences if isinstance(sentences, list) else []


def get_sentence_text(sentence):
    if isinstance(sentence, str):
        return sentence.strip()
    if not isinstance(sentence, dict):
        return ""
    return (sentence.get("text") or sentence.get("sentence") or sentence.get("content") or "").strip()


def get_sentence_number(sentence, fallback):
    if isinstance(sentence, dict):
        value = sentence.get("sentence_id") or sentence.get("sentence_number") or sentence.get("number") or sentence.get("index")
        if isinstance(value, int):
            return value
    return fallback


def result_title(result):
    return str((result or {}).get("title", "") or "").strip()


def result_source(result):
    return str((result or {}).get("source", "") or "").strip().lower()


def result_url(result):
    if not isinstance(result, dict):
        return ""
    return str(result.get("url") or result.get("link") or result.get("video_url") or result.get("image_url") or "").strip()


# ==========================================================================
# STORY BEATS (Stage 2)
# ==========================================================================

def _sentence_place(sentence, bible):
    if not isinstance(sentence, dict):
        return ""
    place = str(sentence.get("place") or "").strip()
    if place:
        return place
    locations = sentence.get("locations") or []
    if locations:
        return str(locations[0]).strip()
    by_id = (bible or {}).get("by_id") or {}
    for entity_id in sentence.get("entity_ids") or []:
        entity = by_id.get(entity_id) or {}
        if entity.get("type") == "place":
            return str(entity.get("canonical") or "").strip()
    return ""


def _sentence_subject(sentence):
    if not isinstance(sentence, dict):
        return ""
    return str(sentence.get("subject_id") or "").strip()


def _sentence_role(sentence):
    if not isinstance(sentence, dict):
        return "background"
    return str(sentence.get("role") or "background").strip().lower()


def _beat_group_key(sentence, bible):
    role = _sentence_role(sentence)
    if role in NO_SEARCH_ROLES:
        return ("__transition__", "", role)
    return (_sentence_subject(sentence), _sentence_place(sentence, bible), "")


def _entity(bible, entity_id):
    if not entity_id or not isinstance(bible, dict):
        return {}
    return (bible.get("by_id") or {}).get(entity_id) or {}


#: Phase 3: which of the plan contract's intent_queries fields feeds
#: T1 query construction for each primary visual intent — the field
#: whose queries describe that intent's own shot (an actual-event
#: beat searches its literal queries; an activity beat its activity
#: B-roll queries; an archival beat its archival queries...).
_INTENT_QUERY_FIELD_BY_PRIMARY = {
    "exact": "literal",
    "actual_event": "literal",
    "archival": "archival",
    "establishing": "establishing",
    "geographic_context": "establishing",
    "activity_broll": "activity_broll",
    "human_activity": "activity_broll",
    "environmental_broll": "contextual",
    "object_detail": "contextual",
    "documentary_context": "contextual",
    "atmospheric": "contextual",
    "graphic_explanation": "contextual",
}

#: Phase 3 / AM-021: a need's T1 query list (composed + intent
#: queries) is capped here so the search's own <=8-queries-per-
#: sentence budget is never crowded out by plan-side merging.
_MAX_NEED_QUERIES = 6


def _bible_entities(bible):
    """The bible's entity records, from whichever shape it carries
    (an ``entities`` list, or the ``by_id`` map's values)."""
    if not isinstance(bible, dict):
        return []
    entities = bible.get("entities")
    if isinstance(entities, list) and entities:
        return [e for e in entities if isinstance(e, dict)]
    by_id = bible.get("by_id")
    if isinstance(by_id, dict):
        return [e for e in by_id.values() if isinstance(e, dict)]
    return []


def _entity_aliases(entity, canonical):
    """An entity's recorded aliases, plus — for a person — the
    surname when the bible did not already record it (parity with
    verify_candidate, which adds the surname to its alias set).
    The gate's discriminative-alias guard decides which of these
    may satisfy a check on their own; this list is evidence, not a
    verdict."""
    aliases = [
        str(a).strip()
        for a in (entity.get("aliases") or [])
        if str(a or "").strip()
    ]
    if str(entity.get("type") or "").strip().lower() == "person":
        parts = _sig_name_tokens(canonical)
        if len(parts) >= 2:
            surname = parts[-1]
            if all(_fold_name(a) != surname for a in aliases):
                # Recorded in its bible surface form where possible:
                # the canonical's last whitespace token.
                surface = str(canonical or "").split()
                aliases.append(surface[-1] if surface else surname)
    return aliases


def _resolve_need_aliases(bible, subject_entity, subject, place):
    """(subject_aliases, place_aliases) for a shot need, resolved
    from the case bible (Transformation Phase 5).

    The subject's aliases come from its own entity record. The
    place's aliases come from the bible entity the need's place
    string identifies — by entity id (a place string that IS an id,
    e.g. "boise_courtroom", resolves to its canonical "Boise
    courtroom" + aliases) or by canonical name. The canonical name
    itself heads the place alias list when it differs from the
    need's place string, so an id-shaped place can match its real
    name in metadata."""
    subject_aliases = []
    if isinstance(subject_entity, dict) and subject_entity:
        subject_aliases = _entity_aliases(subject_entity, subject)
    place_aliases = []
    if place:
        place_entity = None
        for entity in _bible_entities(bible):
            if str(entity.get("id") or "") == str(place):
                place_entity = entity
                break
            if _fold_name(entity.get("canonical")) == _fold_name(place):
                place_entity = entity
                break
        if place_entity is not None:
            canonical = str(place_entity.get("canonical") or "").strip()
            if canonical and _fold_name(canonical) != _fold_name(place):
                place_aliases.append(canonical)
            place_aliases.extend(
                _entity_aliases(place_entity, canonical or place)
            )
    return subject_aliases, place_aliases


def build_shot_needs(beat, bible=None):
    bible = bible or {}

    if not beat.get("searchable", True):
        return []

    banned = [
        str(x)
        for x in (
            bible.get("banned_terms")
            or bible.get("forbidden_terms")
            or []
        )
        if str(x).strip()
    ]

    era = str(
        bible.get("story_era")
        or bible.get("time_period")
        or ""
    ).strip()

    # Transformation Phase 4.2: the bible's genre pack may carry
    # institutional source hints (services.genre_packs). Resolved
    # ONCE per beat here; each need below formats the templates
    # against its own anchors and carries the formatted hint
    # queries for _uve_scenario_search to execute. A bible that
    # names no pack (or a pack without a valid hints block) means
    # no hints — discovery proceeds exactly as before.
    _source_hints = {}
    _pack_id = str(
        (bible.get("research_meta") or {}).get("genre_pack") or ""
    ).strip()
    if _pack_id:
        try:
            from services import genre_packs as _genre_packs

            _resolved_hints = _genre_packs.resolve_source_hints(
                _genre_packs.load_pack(_pack_id)
            )
            _source_hints = _resolved_hints.get("hints") or {}
        except Exception:
            _source_hints = {}

    needs = []

    for index, scenario in enumerate(
        _beat_scenarios(beat),
        start=1,
    ):
        subject_id = (
            scenario.get("subject_id")
            or beat.get("subject_id")
            or ""
        )

        entity = _entity(
            bible,
            subject_id,
        )

        place = str(
            scenario.get("place")
            or beat.get("place")
            or entity.get("location")
            or ""
        ).strip()

        shot_era = str(
            scenario.get("era")
            or era
        ).strip()

        kind = str(
            scenario.get("kind")
            or "archival"
        )

        specificity = str(
            scenario.get("specificity")
            or "representative"
        )

        required_subject = str(
            scenario.get("subject")
            or entity.get("canonical")
            or ""
        ).strip()

        required_type = str(
            scenario.get("required_type")
            or entity.get("type")
            or kind
            or ""
        ).strip()

        event = str(
            scenario.get("event")
            or ""
        ).strip()

        visual_requirement = str(
            scenario.get("visual_requirement")
            or scenario.get("visual")
            or ""
        ).strip()

        description = str(
            scenario.get("visual")
            or ""
        ).strip()

        queries = list(
            scenario.get("queries")
            or []
        )

        # B-roll B1 (§4.2): the owning sentence's fallback concepts
        # ride on the need, stamped with their source. The scenario's
        # _sentence_offset (stamped by _beat_scenarios) names the
        # owning sentence. A sentence record with no recorded
        # concepts (cached/legacy plan) gets the deterministic
        # need-time derivation from its own text and this need's
        # place/era anchors. Recorded and displayed in B1; the
        # escalation loop that searches them is B2.
        _sentence_offset = scenario.get("_sentence_offset")
        _beat_sentences = beat.get("sentences") or []
        _owner_sentence = (
            _beat_sentences[_sentence_offset]
            if isinstance(_sentence_offset, int)
            and 0 <= _sentence_offset < len(_beat_sentences)
            else None
        )
        _concepts = None
        _concepts_source = None
        if isinstance(_owner_sentence, dict):
            _recorded = _owner_sentence.get("fallback_concepts")
            if isinstance(_recorded, dict):
                _concepts = _recorded
                _concepts_source = _owner_sentence.get("concepts_source")
        if _concepts is None:
            _concepts = story.derive_fallback_concepts(
                get_sentence_text(_owner_sentence)
                if _owner_sentence is not None else "",
                place=place,
                era=shot_era,
                entities=[entity] if entity else [],
            )
            _concepts_source = "need_time_derivation"
        # Master Directive Phase 2 (§4.2): the owning sentence's
        # visual-intention record rides beside its concepts (None
        # for pre-Phase-2 plans — the tier ladder then works from
        # concepts alone).
        _intents = None
        if isinstance(_owner_sentence, dict):
            _recorded_intents = _owner_sentence.get("visual_intents")
            if isinstance(_recorded_intents, dict):
                _intents = _recorded_intents

        # Phase 3 / AM-021 + Part 6 Stage B: query provenance, and
        # the intent queries consumed at T1. The scenario's queries
        # keep the source normalize_plan stamped on them
        # ("composed"); unstamped queries were authored on the
        # scenario itself ("scenario"). The primary intent's
        # intent_queries then JOIN the need's list — deduplicated by
        # normalised text, scenario queries first — instead of the
        # search re-deriving facets the plan already carries. Each
        # query's source is recorded on the need (query_sources) and
        # flows into the per-query diagnostics.
        _base_source = (
            "composed"
            if scenario.get("queries_source") == "composed"
            else "scenario"
        )
        query_sources = [
            {"query": q, "source": _base_source} for q in queries
        ]
        if _intents:
            _field = _INTENT_QUERY_FIELD_BY_PRIMARY.get(
                str(_intents.get("primary_visual_intent") or "")
                .strip().lower(),
                "literal",
            )
            _intent_query_list = (
                (_intents.get("intent_queries") or {}).get(_field) or []
            )
            _seen_queries = {
                " ".join(str(q).lower().split()) for q in queries
            }
            for _iq in _intent_query_list:
                _iq_text = str(_iq or "").strip()
                _iq_key = " ".join(_iq_text.lower().split())
                if (
                    _iq_text
                    and _iq_key not in _seen_queries
                    and len(queries) < _MAX_NEED_QUERIES
                ):
                    queries.append(_iq_text)
                    query_sources.append(
                        {"query": _iq_text, "source": "intent_queries"}
                    )
                    _seen_queries.add(_iq_key)

        # Transformation Phase 5: resolve this need's alias
        # evidence once (subject entity + place entity).
        _subject_aliases, _place_aliases = _resolve_need_aliases(
            bible, entity, required_subject, place
        )

        needs.append({
            "beat_id": beat.get("beat_id"),
            "shot_index": index,
            "shot_type": kind,
            "specificity": specificity,
            # Stage 1 / R7: the category profile this need is judged
            # under (explicit scenario category wins; otherwise it
            # derives from specificity). Recorded on the need so the
            # gate, diagnostics and reports all agree on it.
            "visual_category": story.resolve_visual_category(scenario),
            "description": description,
            "required_subject_id": subject_id or None,
            "required_subject": required_subject,
            "required_type": required_type,
            # Transformation Phase 5: the bible's recorded aliases
            # for the subject and place entities — the metadata
            # gate's alias-aware matching evidence (see
            # _subject_match / _place_match). _uve_verify_all
            # re-derives these (setdefault) for needs built by
            # other paths.
            "subject_aliases": _subject_aliases,
            "place_aliases": _place_aliases,
            "subject_entity_type": (
                str(entity.get("type") or "").strip().lower()
                if isinstance(entity, dict) else ""
            ),
            "era": shot_era,
            "place": place,
            "event": event,
            "visual_requirement": visual_requirement,
            "forbidden": banned,
            "priority": (
                1
                if specificity == "exact"
                else (
                    2
                    if specificity == "representative"
                    else 3
                )
            ),
            # B-roll B0 (§3.4): the fossil write-only "fallback"
            # field ("related_entity" / "same_era_place") is
            # superseded by the need's tier record — its ENTRY tier.
            # Every need the pipeline builds today enters at T1, so
            # the stamp is "direct"; derived needs (B2/B3) will
            # carry their own tier. Nothing ever read "fallback".
            "tier": "direct",
            # B-roll B1: the sentence's fallback concepts + source
            # (see above). Recorded, not yet searched.
            "fallback_concepts": _concepts,
            "concepts_source": _concepts_source,
            # Master Directive Phase 2 (§4.2): see above.
            "visual_intents": _intents,
            "queries": queries,
            # Phase 3 / AM-021: per-query provenance, parallel to
            # "queries" (same order): scenario / composed /
            # intent_queries.
            "query_sources": query_sources,
            # Transformation Phase 4.2: institutional source-hint
            # queries for this need, formatted from the genre
            # pack's templates against the need's own anchors
            # ({query} = the need's first query). Executed by
            # _uve_scenario_search on its channel, accounted in
            # the AM-021 diagnostics under source "source_hint".
            # Empty when the pack carries no hints.
            "source_hint_queries": (
                _genre_packs.format_source_hint_queries(
                    _source_hints,
                    subject=required_subject,
                    place=place,
                    era=shot_era,
                    query=queries[0] if queries else "",
                )
                if _source_hints
                else []
            ),
        })

    return needs


def build_story_beats(sentences, bible=None):
    bible = bible or {}
    prepared = []
    for index, sentence in enumerate(sentences or []):
        text = get_sentence_text(sentence)
        if not text:
            continue
        prepared.append({
            "index": index,
            "sentence": sentence,
            "text": text,
            "sentence_number": get_sentence_number(sentence, index + 1),
            "subject_id": _sentence_subject(sentence),
            "place": _sentence_place(sentence, bible),
            "role": _sentence_role(sentence),
            "key": _beat_group_key(sentence, bible),
        })

    beats = []
    current = None
    for item in prepared:
        start_new = (
            current is None
            or item["key"] != current["key"]
            or item["role"] in NO_SEARCH_ROLES
            or current["role"] in NO_SEARCH_ROLES
            or len(current["sentence_indexes"]) >= MAX_BEAT_SENTENCES
        )
        if start_new:
            if current:
                beats.append(current)
            beat_no = len(beats) + 1
            subject = item["subject_id"] or "none"
            place = item["place"] or "none"
            current = {
                "beat_id": f"beat_{beat_no:02d}_{item['role']}_{subject}_{place}",
                "beat_index": beat_no,
                "label": subject if subject != "none" else (item["place"] or item["role"]),
                "subject_id": item["subject_id"],
                "place": item["place"],
                "role": item["role"],
                "searchable": item["role"] not in NO_SEARCH_ROLES,
                "sentence_indexes": [item["index"]],
                "sentence_numbers": [item["sentence_number"]],
                "sentences": [item["sentence"]],
                "text": item["text"],
                "key": item["key"],
            }
        else:
            current["sentence_indexes"].append(item["index"])
            current["sentence_numbers"].append(item["sentence_number"])
            current["sentences"].append(item["sentence"])
            current["text"] = (current["text"] + " " + item["text"]).strip()
    if current:
        beats.append(current)
    for beat in beats:
        beat["shot_needs"] = build_shot_needs(beat, bible)
    return beats

def write_review_log(project_dir, scene_number, beat, records):
    path = Path(project_dir) / f"scene_{scene_number}_review.json"
    existing = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    existing = [row for row in existing if row.get("beat_id") != beat.get("beat_id")]
    existing.append({
        "beat_id": beat.get("beat_id"),
        "role": beat.get("role"),
        "searchable": beat.get("searchable", True),
        "shot_needs": beat.get("shot_needs") or [],
        "records": records,
    })
    write_json_atomic(path, existing)
    print(f"REVIEW SAVED: {path}")
    return path
def write_beats_debug(beats, project_dir, scene_number=1):
    path = Path(project_dir) / f"scene_{scene_number}_beats.json"
    payload = []
    for beat in beats:
        payload.append({
            "beat_id": beat["beat_id"],
            "subject_id": beat.get("subject_id"),
            "place": beat.get("place"),
            "role": beat.get("role"),
            "searchable": beat.get("searchable", True),
            "sentence_numbers": beat.get("sentence_numbers"),
            "text": beat.get("text"),
            "shot_needs": beat.get("shot_needs") or [],
        })
    write_json_atomic(path, payload)
    print(f"BEATS SAVED: {path} count={len(payload)}")
    return path


def _uve_scenarios_for(sentence):
    """
    Build the visual scenario contract from the already-analyzed
    sentence data.

    The LLM/story-understanding layer is the source of truth.
    This function must NOT re-interpret the raw sentence with
    heuristic entity/event extraction.
    """

    if not sentence:
        return []

    # ---------------------------------------------------------
    # Read the existing LLM scenario data.
    # ---------------------------------------------------------

    scenarios = (
        sentence.get("scenarios")
        or sentence.get("visual_scenarios")
        or []
    )

    if isinstance(scenarios, dict):
        scenarios = [scenarios]

    output = []

    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue

        subject = str(
            scenario.get("subject")
            or ""
        ).strip()

        subject_id = str(
            scenario.get("subject_id")
            or ""
        ).strip()

        event = str(
            scenario.get("event")
            or ""
        ).strip()

        place = str(
            scenario.get("place")
            or ""
        ).strip()

        era = str(
            scenario.get("era")
            or ""
        ).strip()

        specificity = str(
            scenario.get("specificity")
            or "representative"
        ).strip()

        kind = str(
            scenario.get("kind")
            or "archival"
        ).strip()

        visual_requirement = str(
            scenario.get("visual_requirement")
            or scenario.get("visual")
            or ""
        ).strip()

        queries = [
            str(q).strip()
            for q in (
                scenario.get("queries")
                or scenario.get("search_terms")
                or []
            )
            if str(q).strip()
        ]

        # -----------------------------------------------------
        # Preserve the LLM's entity type when available.
        # Never infer a subject from the first words of the
        # sentence.
        # -----------------------------------------------------

        required_type = str(
            scenario.get("required_type")
            or ""
        ).strip()

        if not required_type:
            entity_type = str(
                scenario.get("entity_type")
                or ""
            ).strip()

            if entity_type:
                required_type = entity_type
            elif event:
                required_type = "event"
            elif subject:
                required_type = "entity"
            else:
                required_type = kind

        # -----------------------------------------------------
        # De-duplicate queries without modifying their meaning.
        # -----------------------------------------------------

        clean_queries = []
        seen_queries = set()

        for query in queries:
            key = query.lower()

            if key in seen_queries:
                continue

            seen_queries.add(key)
            clean_queries.append(query)

        # -----------------------------------------------------
        # If the LLM supplied no queries, construct only from
        # already-known semantic fields.
        # -----------------------------------------------------

        _queries_constructed = False
        if not clean_queries:
            _queries_constructed = True
            candidates = []

            if subject and event:
                candidates.append(
                    f"{subject} {event}"
                )

            if subject and place:
                candidates.append(
                    f"{subject} {place}"
                )

            if subject and era:
                candidates.append(
                    f"{subject} {era}"
                )

            if subject:
                candidates.append(subject)

            if event and place:
                candidates.append(
                    f"{event} {place}"
                )

            if event:
                candidates.append(event)

            if place and era:
                candidates.append(
                    f"{place} {era}"
                )

            for query in candidates:
                query = " ".join(
                    str(query).split()
                ).strip()

                if not query:
                    continue

                key = query.lower()

                if key in seen_queries:
                    continue

                seen_queries.add(key)
                clean_queries.append(query)

        if not subject and not event and not place and not clean_queries:
            continue

        output.append({
            "visual": str(
                scenario.get("visual")
                or visual_requirement
                or ""
            ).strip(),

            "kind": kind,

            "subject_id": (
                subject_id
                or None
            ),

            "specificity": specificity,

            "search_terms": [
                str(x).strip()
                for x in (
                    scenario.get("search_terms")
                    or []
                )
                if str(x).strip()
            ],

            "event": event,

            "place": place,

            "era": era,

            # F-1 threading: carry the scenario's format intent into the
            # beat loop, where unified selection uses it as a bounded
            # ranking signal.
            "format_intent": str(
                scenario.get("format_intent") or "any"
            ).strip().lower(),

            "format_intent_confidence": scenario.get(
                "format_intent_confidence"
            ) or 0.0,

            "subject": subject,

            "required_type": required_type,

            "visual_requirement": visual_requirement,

            "entities": (
                scenario.get("entities")
                or []
            ),

            "locations": (
                scenario.get("locations")
                or ([place] if place else [])
            ),

            "dates": (
                scenario.get("dates")
                or ([era] if era else [])
            ),

            "objects": (
                scenario.get("objects")
                or []
            ),

            "actions": (
                scenario.get("actions")
                or []
            ),

            "queries": clean_queries[:6],

            # Phase 3 / AM-021: query provenance survives this
            # rebuild — the stamp normalize_plan left on the
            # authored scenario ("composed"), or "composed" when
            # THIS builder constructed the queries from the
            # scenario's semantic fields just above. None when the
            # queries were authored on the scenario itself;
            # build_shot_needs labels those "scenario".
            "queries_source": (
                scenario.get("queries_source")
                or ("composed" if _queries_constructed else None)
            ),
        })

    return output
def _beat_scenarios(beat):
    scenarios = []
    if not beat.get("searchable", True):
        return scenarios
    for offset, sentence in enumerate(beat.get("sentences") or []):
        if _sentence_role(sentence) in NO_SEARCH_ROLES:
            continue
        for scenario in _uve_scenarios_for(sentence):
            # DM-17: remember which sentence this scenario came from,
            # so distribution can deal its assets to that sentence
            # only (see _distribute_beat_assets).
            scenario["_sentence_offset"] = offset
            scenarios.append(scenario)
    unique, seen = [], set()
    for scenario in scenarios:
        key = tuple((scenario.get("queries") or [])[:3])
        if key in seen:
            continue
        seen.add(key)
        unique.append(scenario)
    return unique[:6]


def _beat_scenario_owner_offsets(beat):
    """Map flattened beat scenario_index (1-based, in _beat_scenarios
    order) -> the offset within the beat of the sentence that OWNS
    that scenario, from the _sentence_offset stamps _beat_scenarios
    leaves on each scenario it builds."""
    owners = {}
    for index, scenario in enumerate(_beat_scenarios(beat), start=1):
        offset = scenario.get("_sentence_offset")
        if isinstance(offset, int):
            owners[index] = offset
    return owners


def _distribute_beat_assets(beat, beat_videos, beat_images):
    """Direct per-sentence assignments for a beat (canonical DM-17).

    An asset is dealt ONLY to the sentence that owns the scenario it
    was verified for (its stamped scenario_index). The old code dealt
    beat_videos[offset] / beat_images[offset] by position, so a
    sentence could receive a SIBLING scenario's asset labelled as its
    direct assignment. An asset whose scenario owner is unknown is
    not dealt as direct at all — it stays in the scene aggregates,
    where the timeline may use it only as honestly labelled rotation.
    Returns a list, per sentence offset, of (videos, images)."""
    count = len(beat.get("sentences") or [])
    owners = _beat_scenario_owner_offsets(beat)
    per_sentence = [([], []) for _ in range(count)]

    if not owners:
        # No scenario->sentence linkage exists for this beat (the
        # scenarios did not come from _beat_scenarios — e.g. a caller
        # supplied them directly). DM-17 has nothing to enforce
        # against, so keep the legacy positional dealing rather than
        # dropping verified assets on the floor.
        for offset in range(count):
            if offset < len(beat_videos or []):
                per_sentence[offset][0].append(dict(beat_videos[offset]))
            if offset < len(beat_images or []):
                per_sentence[offset][1].append(dict(beat_images[offset]))
        return per_sentence

    def owner_of(asset):
        try:
            index = int(asset.get("scenario_index"))
        except (TypeError, ValueError):
            return None
        return owners.get(index)

    for asset in beat_videos or []:
        owner = owner_of(asset)
        if owner is not None and owner < count:
            per_sentence[owner][0].append(dict(asset))
    for asset in beat_images or []:
        owner = owner_of(asset)
        if owner is not None and owner < count:
            per_sentence[owner][1].append(dict(asset))
    return per_sentence


def dry_run_beats(script, project_dir, project_id="beats_dry_run", llm="auto"):
    from services.script_analyzer import analyze_script, write_case_bible
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    analysis = analyze_script(
        script,
        project_id=project_id,
        cache_path=str(project_dir / "story_understanding_cache.json"),
        llm=llm,
    )
    write_case_bible(analysis, project_dir)
    bible = story.hydrate_bible(analysis.get("bible"))
    beats = build_story_beats(analysis.get("sentences") or [], bible)
    path = write_beats_debug(beats, project_dir, scene_number=1)
    return {
        "beats_path": str(path),
        "beat_count": len(beats),
        "sentence_count": analysis.get("sentence_count"),
    }


# ==========================================================================
# PROVIDER SEARCH
# ==========================================================================

def kill_process_tree(process):
    # Stage 2G-a: delegate to the portable implementation (Windows
    # keeps taskkill; POSIX kills the child's process group when it
    # is in a different group than us). Kept as a module-level name
    # because run_command_safe calls it.
    process_utils.kill_process_tree(process)


def run_command_safe(command, timeout, label):
    process = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"{label} TIMED OUT ({timeout}s)")
            kill_process_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except Exception:
                stdout, stderr = "", ""
            return -999, stdout or "", stderr or f"{label} timed out"
        return process.returncode, stdout or "", stderr or ""
    except Exception as error:
        print(f"{label} failed to start: {error}")
        if process is not None:
            kill_process_tree(process)
        return -998, "", str(error)


def cleanup_temp_files(directory):
    directory = Path(directory)
    if not directory.exists():
        return
    for pattern in TEMP_FILE_PATTERNS:
        for file in directory.glob(pattern):
            try:
                if file.is_file():
                    file.unlink()
            except OSError:
                pass


def validate_image(path):
    try:
        path = Path(path)
        if not path.exists() or path.stat().st_size < 1000:
            return False
        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img:
            img.load()
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def validate_video(path):
    """A video counts as valid only if ffprobe confirms a real video stream
    with a usable duration (AUD-17). The previous check — file exists and is
    >= 10,000 bytes — let corrupt downloads pass and become silent
    placeholder clips at render time."""
    return probe_video(path) is not None


def _technical_minimums():
    """Configurable technical minimums (VA-12).

    Deliberately conservative: they reject only media that cannot
    plausibly serve a 1080p timeline — sub-second clips and
    thumbnail-grade dimensions. There is NO frame-rate minimum
    (archival footage legitimately runs at unusual rates) and NO
    aspect-ratio rejection (the renderer normalizes). Every value is
    overridable via the environment for unusual productions.
    """
    def _env(name, default):
        try:
            return float(os.environ.get(name) or default)
        except (TypeError, ValueError):
            return float(default)

    return {
        "min_video_width": _env("CINEMORA_MIN_VIDEO_WIDTH", 320),
        "min_video_height": _env("CINEMORA_MIN_VIDEO_HEIGHT", 240),
        "min_video_duration": _env("CINEMORA_MIN_VIDEO_DURATION", 1.0),
        "min_image_width": _env("CINEMORA_MIN_IMAGE_WIDTH", 320),
        "min_image_height": _env("CINEMORA_MIN_IMAGE_HEIGHT", 240),
    }


def _video_technical_failures(probe, minimums=None):
    """Reasons a probed video is technically unsuitable (VA-12).

    Returns a list of failed check names; empty means technically
    acceptable. This is a TECHNICAL judgement only — it says nothing
    about relevance, which the gate/verifier own.
    """
    minimums = minimums or _technical_minimums()
    failures = []
    if probe.get("duration_seconds") is not None and (
        probe["duration_seconds"] < minimums["min_video_duration"]
    ):
        failures.append("duration_below_minimum")
    width, height = probe.get("width"), probe.get("height")
    if width is not None and width < minimums["min_video_width"]:
        failures.append("width_below_minimum")
    if height is not None and height < minimums["min_video_height"]:
        failures.append("height_below_minimum")
    return failures


def _image_technical_failures(probe, minimums=None):
    minimums = minimums or _technical_minimums()
    failures = []
    width, height = probe.get("width"), probe.get("height")
    if width is not None and width < minimums["min_image_width"]:
        failures.append("width_below_minimum")
    if height is not None and height < minimums["min_image_height"]:
        failures.append("height_below_minimum")
    return failures


# Run-11 fix 2: outer attempts for one image download.
# download_image already retries transport errors / 429 / 5xx
# internally (http_retry); this outer loop exists for the failures
# that surface only when the response body is consumed or that a
# fresh connection fixes (premature EOF, connection reset) — and
# it is deliberately small: a persistently dead URL must fail
# fast, and a 429 is not retried here (the rate limit is
# respected, recorded into provider health, and left for the
# scope backoff to govern) — with ONE exception: Wikimedia Commons
# scopes get the Gate 2 bounded delayed retries below, mirroring
# services.licensed_media, because Commons throttling was the
# Container run's dominant download loss.
IMAGE_DOWNLOAD_MAX_ATTEMPTS = 2
_IMAGE_RETRY_BASE_DELAY_SECONDS = 0.5


def _retry_after_seconds(error):
    """The Retry-After value (seconds) an error's response carries,
    or None. Only plain numeric values are read; HTTP-date forms
    are ignored rather than parsed into a guess."""
    headers = getattr(getattr(error, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _wikimedia_delayed_retry(scope, attempt, error=None):
    """Gate 2 retry-before-backoff for Wikimedia Commons scopes
    (footage-discovery directive, 2026-11-26), mirroring
    services.licensed_media._commons_request for the production
    download paths in this module.

    ``attempt`` is the 1-based number of the delayed retry about to
    happen. Returns the bounded delay (seconds) the caller should
    sleep before retrying, recording the retry via
    provider_health.record_delayed_retry; returns None when the
    scope is not a Wikimedia scope or the bounded retries
    (provider_health.WIKIMEDIA_RETRY_ATTEMPTS) are spent — the
    caller then records the rate limit exactly as it did before
    Gate 2. Non-Wikimedia scopes ALWAYS get None: their 429
    behaviour is unchanged (recorded immediately, never retried
    here). The delay honours Retry-After, capped at
    WIKIMEDIA_MAX_RETRY_DELAY_SECONDS, so the retries stay inside
    the caller's existing timeout budget."""
    if not provider_health.is_wikimedia_scope(scope):
        return None
    if attempt > provider_health.WIKIMEDIA_RETRY_ATTEMPTS:
        return None
    delay = provider_health.wikimedia_retry_delay(
        attempt, _retry_after_seconds(error)
    )
    provider_health.record_delayed_retry(scope, delay)
    return delay


def _download_image_detailed(image_url, output_path):
    """Download + validate an image with an explicit reason (VA-13).

    Returns (ok, reason): reason is "" on success, else one of
    no_url / rate_limited / http_error / download_failed /
    undersized_file / decode_failed / probe_failed.
    """
    output_path = Path(output_path)
    if not image_url:
        return False, "no_url"
    error = None
    attempt = 0
    wikimedia_retries = 0
    while True:
        attempt += 1
        try:
            if output_path.exists():
                output_path.unlink()
            download_image(image_url, str(output_path))
            error = None
            break
        except Exception as caught:
            error = caught
            status = getattr(
                getattr(error, "response", None), "status_code", None
            )
            if status == 429 or "429" in str(error):
                # Gate 2: a Wikimedia scope first gets its bounded
                # delayed retries (recorded in provider health,
                # mirroring licensed_media); only a request still
                # 429 after them is recorded as a rate limit. The
                # delayed retry does not consume the outer
                # transport-attempt budget below. Every other
                # scope keeps the Run-11 fix 2 behaviour exactly:
                # the 429 is provider-health evidence (repeated
                # 429s back the scope off for the rest of the
                # production); it is not retried.
                scope = provider_health.scope_for_url(image_url)
                delay = _wikimedia_delayed_retry(
                    scope, wikimedia_retries + 1, error
                )
                if delay is not None:
                    wikimedia_retries += 1
                    attempt -= 1
                    _time.sleep(delay)
                    continue
                provider_health.record_rate_limit(scope)
                break
            if (
                attempt < IMAGE_DOWNLOAD_MAX_ATTEMPTS
                and classify_error(error).get("transient")
            ):
                _time.sleep(_IMAGE_RETRY_BASE_DELAY_SECONDS * attempt)
                continue
            break
    if error is not None:
        print(f"IMAGE DOWNLOAD FAILED: {error}")
        try:
            if output_path.exists():
                output_path.unlink()
        except OSError:
            pass
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status == 429 or "429" in str(error):
            return False, "rate_limited"
        if classify_error(error).get("error_class") == "timeout":
            # Phase 3a / C-02: a timeout is its own class, not a
            # generic download_failed.
            return False, "timeout"
        if status is not None or "HTTP" in type(error).__name__:
            return False, "http_error"
        if re.search(r"\b[45]\d\d\b", str(error)):
            return False, "http_error"
        return False, "download_failed"
    if not output_path.exists() or output_path.stat().st_size < 1000:
        try:
            output_path.unlink()
        except OSError:
            pass
        return False, "undersized_file"
    try:
        with Image.open(output_path) as img:
            img.verify()
        with Image.open(output_path) as img:
            img.load()
    except Exception:
        try:
            output_path.unlink()
        except OSError:
            pass
        return False, "decode_failed"
    return True, ""


def download_valid_image(image_url, output_path):
    ok, _reason = _download_image_detailed(image_url, Path(output_path).resolve())
    return ok


def download_direct_video(url, output_path, duration=DEFAULT_CLIP_DURATION, start_time=0):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Segment-only retrieval FIRST (founder directive): ffmpeg input
    # seeking (-ss BEFORE -i) makes the HTTP client request only the
    # byte ranges around the evidenced segment instead of pulling the
    # whole file. Servers that cannot serve ranges, or sources that
    # fail this path, fall through to the full-download trim below —
    # which is then reported honestly as NOT partial.
    seek_command = [
        "ffmpeg", "-y",
        "-ss", str(max(0, int(start_time or 0))),
        "-i", str(url),
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-an", "-movflags", "+faststart", str(output_path),
    ]
    seek_code, _, _seek_err = run_command_safe(
        seek_command, DOWNLOAD_FFMPEG_TIMEOUT, "FFmpeg range download"
    )
    if seek_code == 0 and validate_video(output_path):
        DOWNLOAD_PROVENANCE[str(output_path)] = {
            "method": "ffmpeg-range-seek",
            "partial": True,
        }
        return True
    try:
        if output_path.exists():
            output_path.unlink()
    except OSError:
        pass

    raw_path = output_path.with_name(output_path.stem + "_raw.mp4")
    try:
        # Run-11 fix 2: the streamed full download gets ONE
        # bounded retry on a transient transport failure (premature
        # EOF / connection reset mid-body — the failure class that
        # killed image downloads in run 11). A completed-but-tiny
        # body is a definitive answer and is not retried; a 429 is
        # recorded into provider health and not retried either —
        # except a Wikimedia scope, which first gets the Gate 2
        # bounded delayed retries (its own counter, mirroring
        # licensed_media) before the 429 is recorded. Wikimedia
        # video derivatives are direct files on
        # upload.wikimedia.org, so THIS is the production path the
        # Container run's Commons losses travelled.
        _stream_error = None
        _transient_retries = 0
        _wikimedia_retries = 0
        while True:
            try:
                response = _http().get(url, timeout=DIRECT_HTTP_TIMEOUT, stream=True)
                response.raise_for_status()
                with open(raw_path, "wb") as handle:
                    for chunk in response.iter_content(64 * 1024):
                        handle.write(chunk)
                _stream_error = None
                break
            except Exception as stream_error:
                _stream_error = stream_error
                _stream_status = getattr(
                    getattr(stream_error, "response", None),
                    "status_code", None,
                )
                if _stream_status == 429 or "429" in str(stream_error):
                    _scope = provider_health.scope_for_url(url)
                    _delay = _wikimedia_delayed_retry(
                        _scope, _wikimedia_retries + 1, stream_error
                    )
                    if _delay is not None:
                        _wikimedia_retries += 1
                        _time.sleep(_delay)
                        continue
                    provider_health.record_rate_limit(_scope)
                    raise
                if not classify_error(stream_error).get("transient"):
                    raise
                if (
                    _transient_retries
                    >= download_taxonomy.MAX_TRANSIENT_RETRIES
                ):
                    raise
                _transient_retries += 1
                try:
                    if raw_path.exists():
                        raw_path.unlink()
                except OSError:
                    pass
        if _stream_error is not None:
            raise _stream_error
        if not raw_path.exists() or raw_path.stat().st_size < 10_000:
            _record_download_error(
                output_path, "invalid_media",
                detail="full download completed below the 10000-byte floor",
            )
            return False
        # MS-3: honour the evidence-backed start_time (was silently
        # ignored — direct downloads always trimmed from second 0).
        command = ["ffmpeg", "-y", "-ss", str(max(0, int(start_time or 0))), "-i", str(raw_path), "-t", str(duration), "-c:v", "libx264",
                   "-preset", "ultrafast", "-crf", "28", "-an", "-movflags", "+faststart", str(output_path)]
        code, _, stderr = run_command_safe(command, DOWNLOAD_FFMPEG_TIMEOUT, "FFmpeg source")
        try:
            raw_path.unlink()
        except OSError:
            pass
        if code != 0 or not validate_video(output_path):
            print((stderr or "")[-1000:])
            _record_download_error(
                output_path, "download_failed", detail=stderr or "",
            )
            return False
        # This path downloaded the whole file before trimming — the
        # provenance says so; it is never dressed up as partial.
        DOWNLOAD_PROVENANCE[str(output_path)] = {
            "method": "full-download-trim",
            "partial": False,
        }
        return True
    except Exception as error:
        print(f"Source video download failed: {error}")
        _record_download_error(
            output_path, f"download_exception: {error}",
            error=error,
        )
        try:
            if raw_path.exists():
                raw_path.unlink()
        except OSError:
            pass
        return False


def blurred_fill_filter_complex():
    """The 1080p normalization filtergraph for downloaded clips
    (run-9 fix 5).

    The historical filter scaled the source down to fit 1920x1080
    and padded the remainder with PLAIN BLACK — every portrait or
    4:3 source reached the film centred on black bars (pillarboxed
    interview clips and posters in run e95ac155). The standard
    documentary treatment instead fills the frame with a blurred,
    slightly darkened copy of the source itself behind the intact
    foreground: no information is cropped, no black bars appear,
    and a 16:9 source is pixel-identical to the old behaviour (the
    foreground covers the whole frame)."""
    return (
        "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,boxblur=20:5,eq=brightness=-0.12[bg];"
        "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
    )


def _source_clip_candidates(output_path):
    """The ``<stem>_source.*`` intermediates a yt-dlp section fetch
    may have left for THIS output stem (never a blanket glob: a
    concurrent attempt may share the directory)."""
    output_path = Path(output_path)
    if not output_path.parent.exists():
        return []
    return [
        item
        for item in output_path.parent.glob(f"{output_path.stem}_source.*")
        if item.is_file() and item.suffix.lower() not in TEMP_FILE_SUFFIXES
    ]


def _probed_duration_covers(path, duration):
    """True when ``path`` probes as a real video whose duration
    covers the requested window (within the salvage tolerance)."""
    probe = probe_video(path)
    if probe is None:
        return False
    probed = probe.get("duration_seconds")
    if probed is None:
        return False
    return probed >= float(duration) - SALVAGE_DURATION_TOLERANCE_SECONDS


def _salvaged_source_clip(output_path, duration):
    """Gate 4 segment-loss fix (diagnosis 12, F1): after a yt-dlp
    timeout kill, the section fetch may already have delivered a
    complete file — in run 4, 9 of 12 timed-out attempts left valid
    8s clips on disk that were discarded solely because the wrapper
    process was killed around/after the bytes arrived. Return the
    largest leftover intermediate that probes as a valid video
    covering the requested window, or None. A truncated stub (the
    run's 801-byte bot-check remnants) probes as no video at all and
    a short partial section fails the duration check, so both stay
    honest download failures."""
    best = None
    for item in _source_clip_candidates(output_path):
        if not _probed_duration_covers(item, duration):
            continue
        if best is None or item.stat().st_size > best.stat().st_size:
            best = item
    return best


def _remove_source_intermediates(output_path):
    """Gate 4 (diagnosis 12, F4): a failed attempt's ``*_source.*``
    intermediates for THIS stem are removed on the failure path —
    left behind, they pollute the scene dir and were miscounted as
    landed downloads. Stem-scoped, so an in-flight attempt sharing
    the directory keeps its own intermediate."""
    for item in _source_clip_candidates(output_path):
        try:
            item.unlink()
        except OSError:
            pass


def download_video_clip(
    video_url,
    output_path,
    start_time=0,
    duration=DEFAULT_CLIP_DURATION,
):
    """
    Download one YouTube segment.

    Segment selection happens upstream.
    This function handles reliable extraction and
    final 1080p normalization.
    """

    output_path = Path(output_path)
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if is_direct_media_url(video_url):
        return download_direct_video(
            video_url,
            output_path,
            duration,
            start_time=start_time,
        )

    try:
        if output_path.exists():
            output_path.unlink()
    except OSError:
        pass

    cleanup_temp_files(output_path.parent)

    section_end = float(start_time) + float(duration)

    output_template = str(
        output_path.with_name(
            output_path.stem + "_source.%(ext)s"
        )
    ).replace("\\", "/")

    command = [
        *_yt_dlp_base_command(),

        "--no-playlist",
        "--no-warnings",
        "--restrict-filenames",

        # Phase 3a / C-12: internal retries are bounded by the
        # taxonomy policy (transient at most once more inside the
        # aggregate YT_DLP_TIMEOUT ceiling); a deterministic
        # failure (404/410/rights) spends no retry at all because
        # yt-dlp returns it as a hard error, not a retryable one.
        "--retries", str(YTDLP_RETRIES),
        "--fragment-retries", str(YTDLP_FRAGMENT_RETRIES),
        "--retry-sleep", "1",
        "--socket-timeout", str(YTDLP_SOCKET_TIMEOUT),

        # Use yt-dlp's normal YouTube client handling.
        # Avoid forcing a single client such as VISIONOS.

        # Prefer downloadable MP4-compatible formats.
        "-f",
        "bv*[height<=720]+ba/"
        "bv*[height<=720]/"
        "b[height<=720]/"
        "best",

        "--download-sections",
        f"*{start_time}-{section_end}",

        "--force-keyframes-at-cuts",

        "--no-part",

        "-o",
        output_template,

        video_url,
    ]

    print(
        f"YOUTUBE CLIP EXTRACTION: "
        f"{video_url} "
        f"start={start_time}s duration={duration}s"
    )

    returncode, stdout, stderr = run_command_safe(
        command,
        YT_DLP_TIMEOUT,
        "yt-dlp",
    )

    # Gate 2 retry-before-backoff, Wikimedia scopes only: a
    # Commons page URL that reaches yt-dlp extraction and fails
    # with a 429 gets the same bounded delayed retries as the
    # HTTP paths (each attempt keeps its own YT_DLP_TIMEOUT; the
    # delay is capped at WIKIMEDIA_MAX_RETRY_DELAY_SECONDS and
    # recorded in provider health). Only an extraction still
    # failing with 429 after those retries is recorded as a rate
    # limit below. Every other scope's failed extraction is
    # handled exactly as before — no retry, immediate recording.
    _clip_scope = provider_health.scope_for_url(video_url)
    _wikimedia_retries = 0
    while (
        returncode != 0
        and "429" in str(stderr or "")
        and provider_health.is_wikimedia_scope(_clip_scope)
        and _wikimedia_retries < provider_health.WIKIMEDIA_RETRY_ATTEMPTS
    ):
        _wikimedia_retries += 1
        _delay = provider_health.wikimedia_retry_delay(_wikimedia_retries)
        provider_health.record_delayed_retry(_clip_scope, _delay)
        print(
            f"yt-dlp 429 from Wikimedia scope {_clip_scope}: "
            f"delayed retry {_wikimedia_retries}/"
            f"{provider_health.WIKIMEDIA_RETRY_ATTEMPTS} "
            f"after {_delay:.1f}s"
        )
        _time.sleep(_delay)
        cleanup_temp_files(output_path.parent)
        returncode, stdout, stderr = run_command_safe(
            command,
            YT_DLP_TIMEOUT,
            "yt-dlp",
        )

    salvaged_clip = None
    if returncode != 0:
        # Gate 4 segment-loss fix (diagnosis 12, F1): a TIMEOUT kill
        # (returncode -999) is not proof the fetch failed — the
        # section bytes may already be on disk as a complete clip.
        # Promote a leftover that probes valid and covers the
        # requested window through the normal normalisation path
        # below instead of recording download_failed. Any other
        # failure (bot-check refusal, 404, ...) — and a timeout that
        # left only a stub or a short partial — fails honestly,
        # exactly as before.
        if returncode == -999:
            salvaged_clip = _salvaged_source_clip(output_path, duration)
        if salvaged_clip is not None:
            print(
                "yt-dlp timed out but left a complete section "
                f"file; promoting {salvaged_clip.name} through "
                "normalisation instead of discarding the segment."
            )
        else:
            print("yt-dlp clip extraction failed:")
            print((stderr or "")[-3000:])
            _record_download_error(
                output_path,
                "timeout" if returncode == -999 else "download_failed",
                detail=stderr or "",
            )
            # Run-11 fix 2: a 429 inside a failed extraction is
            # provider-health evidence for the clip's scope — repeated
            # 429s back the scope off instead of being retried into
            # the ground by later candidates.
            if "429" in str(stderr or ""):
                provider_health.record_rate_limit(
                    provider_health.scope_for_url(video_url)
                )

            cleanup_temp_files(output_path.parent)
            _remove_source_intermediates(output_path)

            return False

    if salvaged_clip is not None:
        candidates = [salvaged_clip]
    else:
        candidates = _source_clip_candidates(output_path)

    if not candidates:
        print(
            "yt-dlp completed but no "
            "source clip was found."
        )
        _record_download_error(
            output_path, "source_unavailable",
            detail="yt-dlp completed but produced no source clip",
        )

        cleanup_temp_files(output_path.parent)

        return False

    candidates.sort(
        key=lambda item: -item.stat().st_size
    )

    source_clip = candidates[0]

    ffmpeg_command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_clip),
        "-t",
        str(duration),
        "-an",
        "-filter_complex",
        blurred_fill_filter_complex(),
        "-map",
        "[v]",
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    returncode, stdout, stderr = run_command_safe(
        ffmpeg_command,
        NORMALIZE_FFMPEG_TIMEOUT,
        "FFmpeg YouTube",
    )

    try:
        source_clip.unlink()
    except OSError:
        pass

    cleanup_temp_files(output_path.parent)

    # Gate 4 segment-loss fix (diagnosis 12, F2): the same
    # salvage-valid-after-timeout rule as the fetch — if the
    # normalisation wrapper was killed at its ceiling but the
    # output it left probes as a valid video covering the whole
    # requested window, the clip is real and is accepted; a
    # truncated output (no moov / short duration) still fails.
    normalized_ok = validate_video(output_path) and (
        returncode == 0
        or (returncode == -999 and _probed_duration_covers(output_path, duration))
    )

    if not normalized_ok:
        print(
            "YouTube clip normalization failed."
        )
        _record_download_error(
            output_path,
            "timeout" if returncode == -999 else "invalid_media",
            detail=stderr or "",
        )

        if stderr:
            print((stderr or "")[-2000:])

        try:
            if output_path.exists():
                output_path.unlink()
        except OSError:
            pass

        return False

    print(
        f"VALID YOUTUBE VIDEO CLIP: "
        f"{output_path}"
    )

    # The yt-dlp path retrieves only the evidenced section
    # (--download-sections), never the whole source video.
    DOWNLOAD_PROVENANCE[str(output_path)] = {
        "method": "yt-dlp-download-sections",
        "partial": True,
        **(
            {"salvaged_after_timeout": True}
            if salvaged_clip is not None
            else {}
        ),
    }

    return True

def download_video_with_ytdlp(video_url, output_path, start_time=0, duration=DEFAULT_CLIP_DURATION):
    return download_video_clip(video_url, output_path, start_time, duration)


def process_sentence_videos(sentence, sentence_dir, video_count=DEFAULT_VIDEO_COUNT):
    # AUD-24: this was a stub that silently returned []. A legacy test
    # script exercised it and "passed" while testing nothing. Fail loudly.
    raise NotImplementedError(
        "process_sentence_videos is a retired stub. Use process_scene_media "
        "(story-driven visual engine) — it assigns verified visuals per sentence."
    )


def process_sentence_images(sentence, sentence_dir, image_count=DEFAULT_IMAGE_COUNT):
    # AUD-24: see process_sentence_videos.
    raise NotImplementedError(
        "process_sentence_images is a retired stub. Use process_scene_media "
        "(story-driven visual engine) — it assigns verified visuals per sentence."
    )


def process_sentence_media(sentence, project_dir, scene_number, image_count=DEFAULT_IMAGE_COUNT, video_count=DEFAULT_VIDEO_COUNT):
    # AUD-24: see process_sentence_videos. This stub returned a plausible-
    # looking but empty result dict, which silently produced visual-less
    # sentences wherever it was called.
    raise NotImplementedError(
        "process_sentence_media is a retired stub. Use process_scene_media "
        "(story-driven visual engine) — it assigns verified visuals per sentence."
    )


# ==========================================================================

# ==========================================================================

def _people_for_sentence(sentence, bible):
    by_id = bible.get("by_id", {}) if isinstance(bible, dict) else {}
    if not isinstance(sentence, dict) or not by_id:
        return []
    mention_ids = story.find_mentions(get_sentence_text(sentence), bible.get("alias_index", {}))
    ids = [e for e in mention_ids if e in by_id and by_id[e].get("type") == "person"]
    for scenario in sentence.get("scenarios", []) or []:
        sid = scenario.get("subject_id")
        if (
            sid in by_id
            and by_id[sid].get("type") == "person"
            and scenario.get("specificity") == "exact"
            and sid not in ids
        ):
            ids.append(sid)
    return [by_id[i]["canonical"] for i in ids if i in by_id]


def _uve_clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _uve_safe_filename(value, max_length=70):
    value = re.sub(r"[^A-Za-z0-9]+", "_", _uve_clean_text(value)).strip("_")
    return (value or "visual")[:max_length]


_yt_segment_uses = {}

_youtube_metadata_cache = {}
_youtube_transcript_cache = {}

# =====================================================================
# YouTube source intelligence: ONE yt-dlp call per URL, persistent cache.
# (added by patch_youtube_info.py)
# =====================================================================
import threading as _threading
import time as _time
from urllib.error import HTTPError as _HTTPError

try:
    from services import source_cache
except ImportError:  # flat-layout fallback
    import source_cache

YT_INFO_TIMEOUT = 40
_yt_info_failed = set()
_yt_transcript_failed = set()
_yt_info_locks = {}
_yt_info_guard = _threading.Lock()


def reset_media_caches():
    """Clear every process-global YouTube cache (AUD-18).

    These caches are keyed by raw URL and were NEVER reset: in the
    long-lived worker, project B inherited project A's used segments and
    permanent failure blacklists, so identical URLs were spuriously
    rejected in unrelated later projects. jobs.run_full_pipeline calls
    this at the start of every production run, alongside the registry
    reset. The persistent source_cache (SQLite) is intentionally NOT
    cleared — it is the cross-project memory; these are per-job only.
    """
    _yt_segment_uses.clear()
    _youtube_metadata_cache.clear()
    _youtube_transcript_cache.clear()
    _yt_info_failed.clear()
    _yt_transcript_failed.clear()
    _yt_info_locks.clear()
    # Run-11 fixes 1+2: redirect resolutions and provider health
    # are per-production state too — a new production re-resolves
    # wrappers and re-earns each provider scope's trust.
    url_hygiene.clear_resolution_cache()
    provider_health.reset()
    _FAILED_DOWNLOAD_URLS.clear()
    _DOWNLOAD_URL_FAILURES.clear()
    _DOWNLOADED_ASSETS_BY_URL.clear()
    _COMMITTED_CONTENT_OWNERS.clear()
    _DISCOVERY_FAILURES.clear()
    _SEGMENT_BASIS.clear()
    _SEGMENT_ATTEMPTS.clear()
    _SEGMENT_FAILURE_CAUSE.clear()
    _SEGMENT_FILE_CACHE.clear()
    # Phase 2: the source-duration cache joins the reset set (it was
    # the one discovery container that survived across productions).
    _SOURCE_DURATION_CACHE.clear()


# =====================================================================
# Stage 2C — RunContext binding for the per-run globals above.
#
# The containers below are rebound (NOT copied) to the active
# RunContext's containers on activation, so every existing read/write
# site in this module lands in the active context with no logic
# changes. The DEFAULT context wraps the ORIGINAL container objects
# created at import time, so behaviour before any activation is
# bit-identical to the old module-global behaviour.
#
# NOT migrated, deliberately:
# - _yt_info_locks / _yt_info_guard: synchronization primitives —
#   swapping lock identity mid-flight would break mutual exclusion.
# - services.source_cache: SQLite profiles are cross-project BY
#   DESIGN (the persistent cross-run memory).
# - services.run_metrics: already run-scoped.
# - visual_registry: project-keyed.
# =====================================================================
try:
    from services import run_context
except ImportError:  # flat-layout fallback
    import run_context

_RUN_CONTEXT_BINDINGS = {
    "_FAILED_DOWNLOAD_URLS": "failed_download_urls",
    "_DISCOVERY_FAILURES": "discovery_failures",
    "_SEGMENT_BASIS": "segment_basis",
    "_SEGMENT_ATTEMPTS": "segment_attempts",
    "_SEGMENT_FAILURE_CAUSE": "segment_failure_cause",
    "_yt_segment_uses": "yt_segment_uses",
    "_youtube_metadata_cache": "youtube_metadata_cache",
    "_youtube_transcript_cache": "youtube_transcript_cache",
    "_yt_info_failed": "yt_info_failed",
    "_yt_transcript_failed": "yt_transcript_failed",
    "_SEGMENT_FILE_CACHE": "segment_file_cache",
    "_search_failure_state": "search_failure_state",
    "_DOWNLOAD_URL_FAILURES": "download_url_failures",
    "_DOWNLOADED_ASSETS_BY_URL": "downloaded_assets_by_url",
    "_COMMITTED_CONTENT_OWNERS": "committed_content_owners",
    "_SOURCE_DURATION_CACHE": "source_duration_cache",
}


def _bind_run_context(ctx):
    """Rebind this module's per-run globals to *ctx*'s containers."""
    if ctx is None:
        return
    module_globals = globals()
    for global_name, context_attr in _RUN_CONTEXT_BINDINGS.items():
        module_globals[global_name] = getattr(ctx, context_attr)


_DEFAULT_RUN_CONTEXT = run_context.RunContext(
    failed_download_urls=_FAILED_DOWNLOAD_URLS,
    discovery_failures=_DISCOVERY_FAILURES,
    segment_basis=_SEGMENT_BASIS,
    segment_attempts=_SEGMENT_ATTEMPTS,
    segment_failure_cause=_SEGMENT_FAILURE_CAUSE,
    yt_segment_uses=_yt_segment_uses,
    youtube_metadata_cache=_youtube_metadata_cache,
    youtube_transcript_cache=_youtube_transcript_cache,
    yt_info_failed=_yt_info_failed,
    yt_transcript_failed=_yt_transcript_failed,
    segment_file_cache=_SEGMENT_FILE_CACHE,
    search_failure_state=_search_failure_state,
    download_url_failures=_DOWNLOAD_URL_FAILURES,
    downloaded_assets_by_url=_DOWNLOADED_ASSETS_BY_URL,
    committed_content_owners=_COMMITTED_CONTENT_OWNERS,
    source_duration_cache=_SOURCE_DURATION_CACHE,
)
run_context.set_default(_DEFAULT_RUN_CONTEXT)
run_context.register_binder(_bind_run_context)


def _youtube_info(key):
    """
    Full yt-dlp info for a URL. One subprocess per URL per process, and
    the useful parts (duration, title, description, chapters) are written
    to the persistent source profile so later runs need no yt-dlp call.
    """
    info = _youtube_metadata_cache.get(key)
    if isinstance(info, dict) and info.get("_full"):
        return info

    if key in _yt_info_failed:
        return None

    with _yt_info_guard:
        lock = _yt_info_locks.setdefault(key, _threading.Lock())

    with lock:
        info = _youtube_metadata_cache.get(key)
        if isinstance(info, dict) and info.get("_full"):
            return info

        if source_cache.yt_breaker_open():
            print(
                f"YOUTUBE INFO SKIPPED (429 cooldown "
                f"{source_cache.yt_breaker_remaining():.0f}s): {key}"
            )
            return None

        source_cache.bump("ytdlp_calls")
        started = _time.time()

        try:
            code, stdout, stderr = run_command_safe(
                [
                    *_yt_dlp_base_command(),
                    "--no-warnings",
                    "--dump-single-json",
                    "--skip-download",
                    key,
                ],
                YT_INFO_TIMEOUT,
                "yt-dlp info",
            )
        except Exception as exc:
            print(f"YOUTUBE INFO FAILED: {key}: {exc}")
            _yt_info_failed.add(key)
            return None

        elapsed = _time.time() - started

        if code != 0 or not stdout:
            if "429" in str(stderr or ""):
                source_cache.yt_breaker_trip(90)
                # Run-11 fix 2: the 429 is also production-scope
                # provider-health evidence — repeated 429s back
                # the scope off for the rest of the production,
                # beyond this breaker's 90-second pause.
                provider_health.record_rate_limit(
                    provider_health.scope_for_url(key)
                )
                print(f"YOUTUBE INFO 429: {key} ({elapsed:.1f}s) - pausing YouTube calls")
            else:
                _yt_info_failed.add(key)
                print(f"YOUTUBE INFO FAILED: {key} ({elapsed:.1f}s)")
            return None

        try:
            info = json.loads(stdout)
        except Exception as exc:
            print(f"YOUTUBE INFO PARSE FAILED: {key}: {exc}")
            _yt_info_failed.add(key)
            return None

        info["_full"] = True
        _youtube_metadata_cache[key] = info

        source_cache.profile_upsert(
            key,
            status="ok",
            duration=info.get("duration"),
            title=str(info.get("title") or ""),
            channel=str(info.get("channel") or info.get("uploader") or ""),
            description=str(info.get("description") or "")[:4000],
            chapters=info.get("chapters") or [],
        )

        print(f"YOUTUBE INFO: {key} ({elapsed:.1f}s)")
        return info


def _youtube_slim(key):
    """duration / chapters / title / description without a yt-dlp call
    when the URL was analysed before (this run or an earlier one)."""
    info = _youtube_metadata_cache.get(key)

    if not (isinstance(info, dict) and info.get("_full")):
        persisted = source_cache.profile_get(key)

        # VA-14: a stale profile (past its TTL) is re-analysed rather
        # than trusted — the video may have become unavailable since.
        if (
            persisted and persisted.get("duration")
            and persisted.get("status") != "stale"
        ):
            return {
                "duration": persisted["duration"],
                "chapters": persisted.get("chapters") or [],
                "title": persisted.get("title") or "",
                "description": persisted.get("description") or "",
            }

        info = _youtube_info(key)

    if not info:
        return None

    return {
        "duration": info.get("duration"),
        "chapters": info.get("chapters") or [],
        "title": info.get("title") or "",
        "description": info.get("description") or "",
    }


def _get_youtube_transcript(url):
    """
    Load the raw yt-dlp json3 transcript for a source URL, or None.

    This is the SINGLE transcript loading path: memory cache ->
    persistent source profile -> one caption download (failures are
    cached in _yt_transcript_failed and never retried per shot).
    _youtube_transcript_segment uses it to find segments, and the
    Stage 3B transcript-interval verification uses it to read the
    text of an already-chosen segment. Fail-soft: any failure
    returns None.
    """
    key = str(url or "").split("&", 1)[0].strip()

    if not key:
        return None

    transcript = _youtube_transcript_cache.get(key)

    if transcript is None:
        if key in _yt_transcript_failed:
            return None

        persisted = source_cache.profile_get(key)

        if persisted and persisted.get("transcript"):
            transcript = persisted["transcript"]
            _youtube_transcript_cache[key] = transcript
        elif persisted and persisted.get("transcript_status") in (
            "unavailable",
            "rate_limited",
        ):
            print(
                f"YOUTUBE TRANSCRIPT SKIPPED "
                f"(cached {persisted['transcript_status']}): {key}"
            )
            return None

    if transcript is None:
        info = _youtube_info(key)

        if not info:
            return None

        manual = info.get("subtitles") or {}
        auto = info.get("automatic_captions") or {}

        tracks = (
            manual.get("en")
            or auto.get("en")
            or auto.get("en-orig")
            or []
        )

        json_track = next(
            (
                track
                for track in tracks
                if str(track.get("ext") or "").lower() == "json3"
            ),
            None,
        )

        caption_url = str(
            (json_track or {}).get("url") or ""
        ).strip()

        if not caption_url:
            print(
                f"YOUTUBE TRANSCRIPT UNAVAILABLE: {key}"
            )
            _yt_transcript_failed.add(key)
            source_cache.profile_upsert(
                key,
                transcript_status="unavailable",
                fail_reason="no english json3 caption track",
            )
            return None

        try:
            from urllib.request import Request, urlopen

            request = Request(
                caption_url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                },
            )

            source_cache.bump("transcript_downloads")

            with urlopen(
                request,
                timeout=30,
            ) as response:
                raw = response.read()

            transcript = json.loads(
                raw.decode(
                    "utf-8",
                    errors="replace",
                )
            )

            _youtube_transcript_cache[key] = transcript

            source_cache.profile_upsert(
                key,
                transcript_status="ok",
                transcript=transcript,
            )

        except _HTTPError as exc:
            if exc.code == 429:
                source_cache.yt_breaker_trip(90)
                # Run-11 fix 2: production-scope provider-health
                # evidence, as at the yt-dlp info path.
                provider_health.record_rate_limit(
                    provider_health.scope_for_url(key)
                )
                source_cache.profile_upsert(
                    key,
                    transcript_status="rate_limited",
                    fail_reason="HTTP 429",
                    retry_after=_time.time() + 600,
                )

            print(
                f"YOUTUBE TRANSCRIPT DOWNLOAD FAILED: "
                f"{exc}"
            )
            _yt_transcript_failed.add(key)
            return None

        except Exception as exc:
            print(
                f"YOUTUBE TRANSCRIPT DOWNLOAD FAILED: "
                f"{exc}"
            )
            _yt_transcript_failed.add(key)
            return None

    return transcript


def _transcript_passages(key):
    """The transcript passages of one video source: [{start, text}].

    Stage 7 / R8 (review C.D.3): when the segment index already
    holds this source's transcript passages, they are read from the
    index — segment EVIDENCE is reused across beats and runs without
    re-calling yt-dlp or re-downloading the transcript. Otherwise
    the passages derive from the single transcript path
    (_get_youtube_transcript: memory cache -> persistent profile ->
    one download; failures are cached, never retried per shot),
    exactly as before. Returns None when no transcript is available
    at all, [] when one exists but yields no passages.
    """
    try:
        indexed = segment_index.indexed_segments(
            canonical_source_key(key)
        )
    except Exception:
        indexed = []
    passages = [
        {"start": float(row["start"]), "text": row.get("text") or ""}
        for row in indexed
        if row.get("basis") == "transcript" and row.get("text")
    ]
    if passages:
        return passages

    transcript = _get_youtube_transcript(key)
    if transcript is None:
        return None
    return _passages_from_transcript(transcript)


def _passages_from_transcript(transcript):
    """[{start, text}] from a json3 transcript payload — the one
    passage construction, shared by live segment selection and by
    the segment-index builder."""
    passages = []
    for event_item in (transcript or {}).get("events") or []:
        segs = event_item.get("segs") or []
        if not segs:
            continue
        text_parts = [
            str(seg.get("utf8") or "")
            for seg in segs
        ]
        passage_text = " ".join(
            part.strip()
            for part in text_parts
            if part.strip()
        ).strip()
        if not passage_text:
            continue
        try:
            start_time = (
                float(event_item.get("tStartMs") or 0) / 1000.0
            )
        except (TypeError, ValueError):
            continue
        passages.append(
            {
                "start": start_time,
                "text": passage_text,
            }
        )
    return passages


def _youtube_transcript_segment(
    url,
    subject="",
    place="",
    event="",
    era="",
):
    """
    Find a relevant timestamp inside a YouTube automatic transcript.

    Evidence is evaluated across a short transcript window rather
    than one caption line. This is important because documentary
    narration often introduces the subject, location, and event
    across several consecutive sentences.

    This function is intentionally niche-agnostic.
    """

    key = str(url or "").split("&", 1)[0].strip()

    if not key:
        return None

    def normalize(value):
        value = " ".join(
            str(value or "").split()
        ).strip().lower()

        value = re.sub(
            r"^(the|a|an)\s+",
            "",
            value,
            flags=re.IGNORECASE,
        )

        return value

    subject = normalize(subject)
    place = normalize(place)
    event = normalize(event)
    era = normalize(era)

    terms = [
        value
        for value in (
            subject,
            place,
            event,
            era,
        )
        if value
    ]

    if not terms:
        return None

    # ---------------------------------------------------------
    # Transcript passages: the segment index when this source was
    # already indexed (Stage 7 / R8), else the single transcript
    # path (memory cache -> persistent cache -> one download;
    # failures are cached, never retried per shot).
    # ---------------------------------------------------------

    passages = _transcript_passages(key)

    if not passages:
        return None

    # ---------------------------------------------------------
    # Build context windows.
    #
    # Each candidate passage gets surrounding transcript
    # context. This allows evidence to appear across several
    # consecutive captions instead of requiring everything
    # to occur in one subtitle line.
    # ---------------------------------------------------------

    window_seconds = 30

    candidates = []

    for index, passage in enumerate(passages):
        start_time = passage["start"]
        window_end = (
            start_time
            + window_seconds
        )

        window_items = []

        for later in passages[index:]:
            if later["start"] > window_end:
                break

            window_items.append(later)

        if not window_items:
            continue

        context_text = " ".join(
            item["text"]
            for item in window_items
        )

        context_normalized = normalize(
            context_text
        )

        if not context_normalized:
            continue

        score = 0
        matched = []

        for term in terms:
            if term in context_normalized:
                score += 10
                matched.append(term)
                continue

            words = [
                word
                for word in term.split()
                if len(word) >= 3
            ]

            if not words:
                continue

            matches = sum(
                1
                for word in words
                if word in context_normalized
            )

            if matches:
                score += matches * 2
                matched.append(term)

        # -----------------------------------------------------
        # Generic event-language support.
        #
        # Documentary narration often expresses an event
        # indirectly:
        #
        # disappearance -> missing / disappeared / vanished
        # death         -> died / killed / dead
        # launch        -> launched / launch
        # landing       -> landed / landing
        #
        # This is intentionally generic and not tied to any
        # particular documentary subject.
        # -----------------------------------------------------

        event_aliases = {
            "disappearance": (
                "disappeared",
                "disappear",
                "missing",
                "vanished",
                "last seen",
            ),
            "death": (
                "died",
                "dead",
                "killed",
                "murdered",
            ),
            "launch": (
                "launched",
                "launching",
                "launch",
            ),
            "landing": (
                "landed",
                "landing",
            ),
            "flood": (
                "flooded",
                "flooding",
                "flood",
            ),
            "arrest": (
                "arrested",
                "arrest",
                "detained",
            ),
        }

        normalized_event = event

        aliases = event_aliases.get(
            normalized_event,
            (),
        )

        alias_matches = [
            alias
            for alias in aliases
            if alias in context_normalized
        ]

        if alias_matches:
            score += 6
            matched.extend(
                alias_matches[:3]
            )

        # -----------------------------------------------------
        # Strong exact-contract rule.
        #
        # An exact subject/location contract requires the
        # subject OR its meaningful words plus the location.
        # Event aliases can satisfy the event component.
        # -----------------------------------------------------

        # -----------------------------------------------------
        # Primary evidence is the subject.
        #
        # Supporting fields such as place or era do not always
        # appear in the same narration sentence. A documentary
        # may introduce a person first and explain the location
        # several sentences later.
        # -----------------------------------------------------

        if subject:
            subject_present = (
                subject in context_normalized
                or all(
                    word in context_normalized
                    for word in subject.split()
                    if len(word) >= 3
                )
            )

            if not subject_present:
                continue

        # -----------------------------------------------------
        # For an event contract, require either the exact event
        # or a recognized event-language equivalent.
        # -----------------------------------------------------

        if event:
            event_present = (
                event in context_normalized
                or bool(alias_matches)
                or all(
                    word in context_normalized
                    for word in event.split()
                    if len(word) >= 3
                )
            )

            if not event_present:
                continue

        # -----------------------------------------------------
        # Location is supporting evidence.
        #
        # If it is present, reward it. If it is absent from this
        # particular 30-second narration window, do not throw
        # away otherwise strong subject/event evidence.
        # -----------------------------------------------------

        if place:
            place_present = (
                place in context_normalized
                or all(
                    word in context_normalized
                    for word in place.split()
                    if len(word) >= 3
                )
            )

            if place_present:
                score += 5
                matched.append(
                    f"place:{place}"
                )

        # -----------------------------------------------------
        # Evidence-quality bonuses.
        #
        # Prefer windows where the primary subject and event
        # appear close together. This is stronger evidence than
        # a window that merely mentions the subject while
        # discussing an unrelated part of the story.
        # -----------------------------------------------------

        subject_positions = []
        event_positions = []

        if subject:
            position = context_normalized.find(subject)

            if position >= 0:
                subject_positions.append(position)

        if event:
            position = context_normalized.find(event)

            if position >= 0:
                event_positions.append(position)

        for alias in aliases:
            position = context_normalized.find(alias)

            if position >= 0:
                event_positions.append(position)

        if subject_positions and event_positions:
            distance = min(
                abs(
                    subject_position
                    - event_position
                )
                for subject_position in subject_positions
                for event_position in event_positions
            )

            if distance <= 160:
                score += 12
            elif distance <= 400:
                score += 6

        # Exact subject phrase is stronger than scattered
        # individual-name matches.
        if subject and subject in context_normalized:
            score += 5

        # Prefer direct event wording over a weak alias.
        if event and event in context_normalized:
            score += 5

        if score <= 0:
            continue

        candidates.append(
            {
                "score": score,
                "start": start_time,
                "text": context_text,
                "matched": matched,
            }
        )

    if not candidates:
        print(
            f"YOUTUBE TRANSCRIPT NO MATCH: "
            f"{key} terms={terms}"
        )
        return None

    candidates.sort(
        key=lambda item: (
            -item["score"],
            item["start"],
        )
    )

    # Keep a broad pool of strong transcript regions. The caller
    # may request multiple clips from the same YouTube source,
    # so we must not collapse the entire video down to one
    # timestamp.
    candidates = candidates[:40]

    used = _yt_segment_uses.setdefault(
        key,
        [],
    )

    duration = int(
        DEFAULT_CLIP_DURATION
    )

    # Keep clips from the same long source visually separated.
    # This prevents three consecutive clips from becoming one
    # repeated scene broken into 8-second pieces.
    minimum_gap = max(
        duration * 3,
        24,
    )

    for candidate in candidates:
        start_time = int(
            max(0, candidate["start"])
        )

        end_time = (
            start_time
            + duration
        )

        overlaps = any(
            start_time < used_end
            and end_time > used_start
            for used_start, used_end in used
        )

        too_close = any(
            abs(
                start_time
                - used_start
            ) < minimum_gap
            for used_start, used_end in used
        )

        if overlaps or too_close:
            continue

        used.append(
            (
                start_time,
                end_time,
            )
        )

        print(
            f"YOUTUBE TRANSCRIPT SEGMENT: "
            f"{key} "
            f"start={start_time}s "
            f"score={candidate['score']} "
            f"matched={candidate['matched']} "
            f"text='{candidate['text'][:240]}'"
        )

        return start_time

    print(
        f"YOUTUBE TRANSCRIPT EXHAUSTED: "
        f"{key}"
    )

    return None


# =====================================================================
# Stage 3B — transcript-interval verification (review §2.4)
#
# verify_candidate judges a candidate by its METADATA (title /
# snippet / description). This layer judges the actual spoken words
# inside the segment that was chosen: the transcript text overlapping
# [clip_start, clip_start + duration] must contain at least one of
# the scenario's subject/topical tokens. It can ONLY demote — a
# mismatch rejects an otherwise-accepted candidate; a match or an
# unavailable transcript never changes a verdict, and a candidate the
# verifier already rejected is never upgraded.
# =====================================================================

TRANSCRIPT_MIN_WINDOW_TOKENS = 5


def _transcript_window_text(url, start, duration=DEFAULT_CLIP_DURATION):
    """Transcript text overlapping [start, start+duration] for a
    source video, or None when no transcript is available.

    Reuses the single transcript loading path
    (_get_youtube_transcript: memory cache -> persistent profile ->
    one download) — no second fetcher. Transcript entries are the
    json3 events the loader returns; an entry overlaps the window
    when its [tStartMs, tStartMs + dDurationMs] interval intersects
    it (an entry with no duration counts when its start falls inside
    the window). Returns "" when a transcript exists but no entry
    overlaps the window.
    """
    try:
        start = float(start)
    except (TypeError, ValueError):
        return None
    try:
        duration = float(duration or DEFAULT_CLIP_DURATION)
    except (TypeError, ValueError):
        duration = float(DEFAULT_CLIP_DURATION)
    if duration <= 0:
        duration = float(DEFAULT_CLIP_DURATION)

    transcript = _get_youtube_transcript(url)
    if not isinstance(transcript, dict):
        return None

    window_end = start + duration
    parts = []
    for event_item in transcript.get("events") or []:
        segs = event_item.get("segs") or []
        if not segs:
            continue
        text = " ".join(
            part.strip()
            for part in (str(seg.get("utf8") or "") for seg in segs)
            if part.strip()
        ).strip()
        if not text:
            continue
        try:
            ev_start = float(event_item.get("tStartMs") or 0) / 1000.0
        except (TypeError, ValueError):
            continue
        raw_duration = event_item.get("dDurationMs")
        try:
            ev_end = ev_start + float(raw_duration) / 1000.0 if raw_duration else ev_start
        except (TypeError, ValueError):
            ev_end = ev_start
        if ev_end > ev_start:
            overlaps = ev_start < window_end and ev_end > start
        else:
            overlaps = start <= ev_start < window_end
        if overlaps:
            parts.append(text)
    return " ".join(parts)


def _transcript_check_terms(scenario, bible):
    """Subject/topical content tokens for the interval check.

    Mirrors verify_candidate's term extraction in
    story_understanding: the scenario subject's canonical name and
    aliases, the scenario's subject label, its search_terms and its
    visual description — all through the same _content_tokens
    machinery, so the interval check and the verifier speak the
    same token language.
    """
    scenario = scenario or {}
    terms = set()
    by_id = (bible or {}).get("by_id") or {}
    subject = by_id.get(scenario.get("subject_id"))
    if subject:
        terms |= story._content_tokens(subject.get("canonical"))
        for alias in subject.get("aliases") or []:
            terms |= story._content_tokens(alias)
    if scenario.get("subject"):
        terms |= story._content_tokens(scenario.get("subject"))
    for term in scenario.get("search_terms") or []:
        terms |= story._content_tokens(term)
    terms |= story._content_tokens(scenario.get("visual"))
    return terms


def check_transcript_interval(window_text, scenario, bible=None):
    """Judge one transcript window against a scenario.

    - "no_transcript" (NEUTRAL): no window text, or the window
      yields fewer than TRANSCRIPT_MIN_WINDOW_TOKENS content tokens,
      or the scenario defines no subject/topical terms to check.
    - "interval_transcript_match": the window has enough tokens and
      at least one subject/topical token appears in it.
    - "transcript_mismatch": the window has enough tokens and ZERO
      subject/topical tokens appear — the segment is about something
      else, whatever its metadata claimed.
    """
    if window_text is None:
        return "no_transcript"
    window_tokens = story._content_tokens(window_text)
    if len(window_tokens) < TRANSCRIPT_MIN_WINDOW_TOKENS:
        return "no_transcript"
    terms = _transcript_check_terms(scenario, bible)
    if not terms:
        return "no_transcript"
    if terms & window_tokens:
        return "interval_transcript_match"
    return "transcript_mismatch"


def _apply_transcript_check(asset, scenario, bible):
    """Run the Stage 3B check on one downloaded asset.

    Applies only to candidates that (a) passed verify_candidate,
    (b) carry real segment evidence (segment_evidence is not
    "none"/empty) and (c) have a source-video transcript. Stamps
    asset["verification"]["transcript_check"] whenever the check
    runs. On "transcript_mismatch" the verdict is demoted in place
    (accepted=False, rejection_reason="transcript_mismatch") and the
    label is returned; every other outcome leaves the verdict
    untouched. Returns None when the check does not apply or cannot
    run (fail-soft). This layer ONLY ever demotes.
    """
    if not isinstance(asset, dict):
        return None
    verification = asset.get("verification") or {}
    if not verification.get("accepted"):
        return None
    segment_evidence = str(asset.get("segment_evidence") or "").strip()
    if not segment_evidence or segment_evidence == "none":
        return None
    url = str(asset.get("video_url") or asset.get("url") or "").strip()
    start = asset.get("clip_start")
    if not url or start is None:
        return None
    duration = (
        asset.get("clip_duration")
        or asset.get("duration_seconds")
        or DEFAULT_CLIP_DURATION
    )
    try:
        window_text = _transcript_window_text(url, start, duration)
        result = check_transcript_interval(window_text, scenario, bible)
    except Exception as error:  # a transcript failure never crashes a pick
        print(f"TRANSCRIPT CHECK SKIPPED (error): {url}: {error}")
        return None
    stamped = dict(verification)
    stamped["transcript_check"] = result
    if result == "transcript_mismatch":
        stamped["accepted"] = False
        stamped["rejection_reason"] = "transcript_mismatch"
        asset["reject_reason"] = "transcript_mismatch"
        asset["rejection_stage"] = "transcript_verification"
    asset["verification"] = stamped
    return result


# =====================================================================
# Evidence-based timestamp fallback helpers.
# (added by patch_youtube_fallback.py)
# =====================================================================
# Set False to reject sources instead of ever using a low-confidence window.
# AUD-16 (founder directive 2026-11-19): set to False permanently. An
# arbitrary 20/40/60/80% window of a video whose title merely mentions the
# subject is NOT verified footage; sources without timestamp evidence are
# rejected so the caller can try the next candidate.
YT_LOW_CONFIDENCE_FALLBACK = False
# Fractions of the video (0..1) tried, in order, for low-confidence windows.
# The intro (first 20%) and outro (last 20%) are skipped on purpose.
YT_LOW_CONFIDENCE_WINDOWS = (0.2, 0.4, 0.6, 0.8)

_YT_TS_LINE = re.compile(
    r"^\s*[-*\u2022]?\s*\(?((?:\d{1,2}:)?\d{1,2}:\d{2})\)?\s*[-:.)\u2013\u2014]*\s*(.+?)\s*$"
)


def _yt_norm_text(value):
    return " ".join(str(value or "").split()).strip().lower()


def _yt_term_score(text, terms):
    """Same scoring idea as the chapter scorer: exact term +10,
    individual words +2 each."""
    text = _yt_norm_text(text)
    score = 0
    matched = []

    for term in terms:
        if not term:
            continue

        if term in text:
            score += 10
            matched.append(term)
            continue

        words = [w for w in term.split() if len(w) >= 3]
        hits = sum(1 for w in words if w in text)

        if words and hits:
            score += hits * 2
            matched.append(term)

    return score, matched


def _yt_phrase_present(text, phrase):
    text = _yt_norm_text(text)
    phrase = _yt_norm_text(phrase)

    if not phrase:
        return False

    if phrase in text:
        return True

    words = [w for w in phrase.split() if len(w) >= 3]

    return bool(words) and all(w in text for w in words)


def _youtube_description_moments(description):
    """Parse '12:34 Something happens' lines -> [(seconds, text), ...]."""
    moments = []

    for line in str(description or "").splitlines():
        match = _YT_TS_LINE.match(line)

        if not match:
            continue

        seconds = 0

        for part in match.group(1).split(":"):
            seconds = seconds * 60 + int(part)

        moments.append((seconds, match.group(2)))

    return moments


def _youtube_start(
    url,
    subject="",
    place="",
    event="",
    era="",
):
    """
    Choose a safe, relevant, non-overlapping segment from a YouTube source.

    This function is intentionally niche-agnostic.
    It does not know about specific people, places, events, industries,
    historical periods, or documentary topics.

    Relevance is determined only from the supplied visual contract:
        subject
        place
        event
        era

    Returns:
        int  -> valid clip start time
        None -> no suitable unused segment was found
    """

    key = str(url or "").split("&", 1)[0].strip()

    if not key:
        return None

    used = _yt_segment_uses.setdefault(key, [])
    duration = int(DEFAULT_CLIP_DURATION)

    # ---------------------------------------------------------
    # Normalize contract terms.
    # ---------------------------------------------------------

    def normalize(value):
        value = " ".join(
            str(value or "").split()
        ).strip().lower()

        value = re.sub(
            r"^(the|a|an)\s+",
            "",
            value,
            flags=re.IGNORECASE,
        )

        return value.strip()

    subject = normalize(subject)
    place = normalize(place)
    event = normalize(event)
    era = normalize(era)

    contract_terms = []

    for value in (
        subject,
        place,
        event,
        era,
    ):
        if value and value not in contract_terms:
            contract_terms.append(value)

    # ---------------------------------------------------------
    # Get the real YouTube duration.
    # ---------------------------------------------------------

    slim = _youtube_slim(key)

    video_duration = None

    if slim and slim.get("duration"):
        try:
            video_duration = int(float(slim["duration"]))
        except (TypeError, ValueError):
            video_duration = None

    # Chapters come from the same single lookup as duration.
    chapters = list((slim or {}).get("chapters") or [])

    if not video_duration or video_duration <= 0:
        print(
            f"YOUTUBE SEGMENT REJECTED: "
            f"unknown duration: {key}"
        )
        return None

    max_start = max(
        0,
        video_duration - duration,
    )

    print(
        f"YOUTUBE SOURCE DURATION: "
        f"{key} duration={video_duration}s "
        f"max_start={max_start}s"
    )

    # ---------------------------------------------------------
    # Segment overlap protection.
    # ---------------------------------------------------------

    def overlaps(start_time):
        end_time = start_time + duration

        for used_start, used_end in used:
            if (
                start_time < used_end
                and end_time > used_start
            ):
                return True

        return False

    def valid_start(start_time):
        try:
            start_time = int(start_time)
        except (TypeError, ValueError):
            return False

        if start_time < 0:
            return False

        if start_time > max_start:
            return False

        if overlaps(start_time):
            return False

        return True

    def register(start_time):
        used.append(
            (
                int(start_time),
                int(start_time) + duration,
            )
        )

    # ---------------------------------------------------------
    # Read YouTube chapters.
    # ---------------------------------------------------------

    # Chapters were loaded above together with the duration
    # (one yt-dlp call per URL, persisted).

    scored_chapters = []

    for chapter in chapters:
        title = str(
            chapter.get("title") or ""
        ).strip()

        if not title:
            continue

        try:
            start_time = int(
                float(
                    chapter.get("start_time") or 0
                )
            )
        except (TypeError, ValueError):
            continue

        if not valid_start(start_time):
            continue

        title_normalized = normalize(title)

        # -------------------------------------------------
        # Generic contract scoring.
        #
        # Exact multi-word terms are strongest.
        # Individual words provide secondary matching.
        # -------------------------------------------------

        score = 0
        matched_terms = []

        for term in contract_terms:
            if not term:
                continue

            if term in title_normalized:
                score += 10
                matched_terms.append(term)
                continue

            words = [
                word
                for word in term.split()
                if len(word) >= 3
            ]

            word_matches = sum(
                1
                for word in words
                if word in title_normalized
            )

            if words and word_matches:
                score += word_matches * 2
                matched_terms.append(term)

        scored_chapters.append({
            "score": score,
            "start": start_time,
            "title": title,
            "matched_terms": matched_terms,
        })

    chapters = scored_chapters

    chapters.sort(
        key=lambda item: (
            -item["score"],
            item["start"],
        )
    )

    # ---------------------------------------------------------
    # Strict contract:
    #
    # If we know what the shot is supposed to show,
    # do NOT grab an arbitrary chapter just because the
    # source video itself looked relevant.
    # ---------------------------------------------------------

    strict_contract = bool(
        subject
        or place
        or event
        or era
    )

    for chapter in chapters:
        if chapter["score"] <= 0:
            continue

        start_time = chapter["start"]

        if not valid_start(start_time):
            continue

        register(start_time)

        print(
            f"YOUTUBE SEGMENT: {key} "
            f"start={start_time}s "
            f"end={start_time + duration}s "
            f"chapter='{chapter['title']}' "
            f"score={chapter['score']} "
            f"matched={chapter['matched_terms']}"
        )
        _SEGMENT_BASIS[key] = "chapter"
        return start_time

    # ---------------------------------------------------------
    # No matching chapter.
    #
    # Try the timed YouTube transcript before rejecting the
    # source. This allows long videos without chapters to yield
    # the exact section where the requested subject/event/place
    # is discussed.
    # ---------------------------------------------------------

    transcript_start = _youtube_transcript_segment(
        key,
        subject=subject,
        place=place,
        event=event,
        era=era,
    )

    if transcript_start is not None:
        # _youtube_transcript_segment() already registered
        # the range in _yt_segment_uses.
        print(
            f"YOUTUBE SEGMENT: {key} "
            f"start={transcript_start}s "
            f"end={transcript_start + duration}s "
            f"source=transcript"
        )
        _SEGMENT_BASIS[key] = "transcript"
        return transcript_start

    # ---------------------------------------------------------
    # No exact chapter or transcript match.
    #
    # IMPORTANT:
    # The YouTube source has already passed the upstream
    # search + verification gates. Therefore, when this is
    # the same long source being reused for another shot,
    # do not reject it simply because the next segment does
    # not contain the exact contract words in its transcript.
    #
    # First try another unused chapter. Chapters are preferred
    # because they represent meaningful sections of the source.
    # ---------------------------------------------------------

    if strict_contract:
        # -----------------------------------------------------
        # Evidence-based fallback.
        #
        # Replaces "any unused chapter, then the first free slot
        # from 0". A segment is only chosen when there is a reason:
        #
        #   1. a timestamp listed in the video description matches
        #      the contract
        #   2. low-confidence spaced window, only when the source
        #      itself is about the most specific contract term
        #
        # Otherwise the source is rejected for this shot and the
        # caller can try a better source.
        # -----------------------------------------------------

        source_title = str((slim or {}).get("title") or "")
        source_description = str((slim or {}).get("description") or "")

        description_hits = []

        for moment_start, moment_text in _youtube_description_moments(
            source_description
        ):
            moment_score, moment_matched = _yt_term_score(
                moment_text,
                contract_terms,
            )

            if moment_score > 0:
                description_hits.append(
                    (moment_score, moment_start, moment_text, moment_matched)
                )

        description_hits.sort(
            key=lambda item: (-item[0], item[1])
        )

        for moment_score, moment_start, moment_text, moment_matched in description_hits:
            if not valid_start(moment_start):
                continue

            register(moment_start)

            print(
                f"YOUTUBE SEGMENT: {key} "
                f"start={moment_start}s "
                f"end={moment_start + duration}s "
                f"source=description "
                f"line='{moment_text[:80]}' "
                f"score={moment_score} "
                f"matched={moment_matched}"
            )
            _SEGMENT_BASIS[key] = "description"
            return moment_start

        most_specific = event or place or subject

        source_has_evidence = _yt_phrase_present(
            f"{source_title} {source_description[:1500]}",
            most_specific,
        )

        if YT_LOW_CONFIDENCE_FALLBACK and source_has_evidence:
            minimum_gap = max(duration * 3, 24)

            for fraction in YT_LOW_CONFIDENCE_WINDOWS:
                candidate = int(max_start * fraction)

                if not valid_start(candidate):
                    continue

                if any(
                    abs(candidate - used_start) < minimum_gap
                    for used_start, _used_end in used
                ):
                    continue

                register(candidate)

                print(
                    f"YOUTUBE SEGMENT: {key} "
                    f"start={candidate}s "
                    f"end={candidate + duration}s "
                    f"fallback=low_confidence "
                    f"window={fraction} "
                    f"evidence='{most_specific}' "
                    f"subject='{subject}' "
                    f"place='{place}' "
                    f"event='{event}'"
                )

                _SEGMENT_BASIS[key] = "positional_no_contract"
                return candidate

        print(
            f"YOUTUBE SEGMENT REJECTED: no timestamp evidence: {key} "
            f"subject='{subject}' place='{place}' event='{event}' "
            f"source_evidence={source_has_evidence} "
            f"used_segments={len(used)}"
        )

        return None

    # ---------------------------------------------------------
    # No specific contract.
    #
    # This is allowed for genuinely representative footage.
    # Use evenly spaced positions rather than old hard-coded
    # topic-specific timestamps.
    # ---------------------------------------------------------

    if not chapters:
        candidate = 0

        while candidate <= max_start:
            if valid_start(candidate):
                register(candidate)

                print(
                    f"YOUTUBE SEGMENT: {key} "
                    f"start={candidate}s "
                    f"end={candidate + duration}s "
                    f"representative=True"
                )

                _SEGMENT_BASIS[key] = "positional_no_contract"
                return candidate

            candidate += duration

    # ---------------------------------------------------------
    # Chapters exist but none matched and no strict contract.
    # Use an unused chapter.
    # ---------------------------------------------------------

    for chapter in chapters:
        start_time = chapter["start"]

        if not valid_start(start_time):
            continue

        register(start_time)

        print(
            f"YOUTUBE SEGMENT: {key} "
            f"start={start_time}s "
            f"end={start_time + duration}s "
            f"chapter='{chapter['title']}' "
            f"representative=True"
        )
        _SEGMENT_BASIS[key] = "chapter"
        return start_time

    # ---------------------------------------------------------
    # Source exhausted.
    # ---------------------------------------------------------

    print(
        f"YOUTUBE SOURCE EXHAUSTED: {key} "
        f"duration={video_duration}s "
        f"used_segments={len(used)}"
    )

    return None


def _remote_duration_seconds(url):
    """Duration of a remote media file via ffprobe, or None.

    probe_video() is deliberately local-only (it verifies downloaded
    files); segment evidence for a direct URL needs the SOURCE duration
    before any download decision, so it is probed here, bounded.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", str(url),
            ],
            capture_output=True, text=True, timeout=25,
        )
        if result.returncode != 0:
            return None
        info = json.loads(result.stdout or "{}")
        value = (info.get("format") or {}).get("duration")
        return float(value) if value else None
    except Exception:
        return None


def _provider_duration_seconds(candidate):
    """Duration claimed by the candidate's own provider metadata (VA-03).

    Returns (seconds_or_None, source_label). Accepts plain seconds and
    HH:MM:SS / MM:SS strings; anything else is ignored, never guessed.
    This is a FALLBACK for establishing a source's duration when the
    primary channels (remote probe / yt-dlp info) cannot — it is used
    for the whole-asset determination and to bound the labelled
    positional window a long direct file may take (Gate 2); it is
    never used to fabricate timed (transcript/chapter) evidence.
    """
    raw = candidate.get("duration_seconds")
    if raw is None:
        raw = candidate.get("duration")
    value = None
    if isinstance(raw, (int, float)) and raw > 0:
        value = float(raw)
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            value = float(text)
        except ValueError:
            try:
                nums = [float(part) for part in text.split(":")]
                if len(nums) == 2:
                    value = nums[0] * 60 + nums[1]
                elif len(nums) == 3:
                    value = nums[0] * 3600 + nums[1] * 60 + nums[2]
            except ValueError:
                value = None
    if value and value > 0:
        return value, str(
            candidate.get("provider") or candidate.get("source") or "provider"
        )
    return None, ""


def _build_segment_index(url, slim):
    """Normalise one source's chapters, transcript passages and
    description moments into candidate segments (Stage 7 / R8,
    review C.D.3). Each segment records its basis and its evidence
    text — and NO verdict. Chapters come first, then transcript
    passages, then description moments (the store applies the
    per-source cap in that priority order)."""
    key = str(url or "").split("&", 1)[0].strip()
    duration = None
    if slim and slim.get("duration"):
        try:
            duration = float(slim["duration"])
        except (TypeError, ValueError):
            duration = None
    segments = []

    chapters = []
    for chapter in (slim or {}).get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        try:
            start = float(chapter.get("start_time"))
        except (TypeError, ValueError):
            continue
        chapters.append(
            {"start": start, "title": str(chapter.get("title") or "")}
        )
    chapters.sort(key=lambda c: c["start"])
    for i, chapter in enumerate(chapters):
        end = (
            chapters[i + 1]["start"]
            if i + 1 < len(chapters)
            else (duration or chapter["start"] + DEFAULT_CLIP_DURATION)
        )
        segments.append({
            "start": chapter["start"], "end": end,
            "basis": "chapter", "text": chapter["title"],
        })

    transcript = _get_youtube_transcript(key)
    raw_passages = _passages_from_transcript(transcript)
    for i, passage in enumerate(raw_passages):
        if i + 1 < len(raw_passages):
            end = raw_passages[i + 1]["start"]
        elif duration:
            end = min(passage["start"] + 30.0, duration)
        else:
            end = passage["start"] + 30.0
        segments.append({
            "start": passage["start"], "end": max(end, passage["start"]),
            "basis": "transcript", "text": passage["text"],
        })

    if slim and slim.get("description"):
        for seconds, text in _youtube_description_moments(
            slim["description"]
        ):
            segments.append({
                "start": float(seconds),
                "end": float(seconds) + DEFAULT_CLIP_DURATION,
                "basis": "description", "text": text,
            })
    return segments


def _ensure_segment_index(url, slim):
    """Build the source's segment index if it has none (C.D.3: built
    once per source, when the source first reaches segment
    selection). Fail-soft by design: an index failure is recorded in
    the source's segment attempts and never blocks selection."""
    key = str(url or "").split("&", 1)[0].strip()
    if not key:
        return
    try:
        segments, built = segment_index.ensure_segment_index(
            canonical_source_key(key),
            lambda: _build_segment_index(url, slim),
        )
    except Exception as error:
        attempts = _SEGMENT_ATTEMPTS.get(url)
        if attempts is not None:
            attempts.append({
                "source": "segment_index", "outcome": "failed",
                "error": str(error)[:120],
            })
        return
    if built:
        attempts = _SEGMENT_ATTEMPTS.get(url)
        if attempts is not None:
            attempts.append({
                "source": "segment_index", "outcome": "built",
                "segments": len(segments),
            })


def _positional_direct_window(url, source_duration, clip):
    """Deterministic positional window for a long direct-media file
    (Gate 2 segment-evidence unlock).

    A direct file (archive.org / Wikimedia / institutional MP4) has
    no timed-evidence channel — no transcript, chapters or
    description moments to match a contract against — so the only
    honest segment is a POSITIONAL one: a clip-length window whose
    position is a fact about the file, not a claim about its
    content. Windows step deterministically from the start of the
    file by ``clip`` seconds, skipping windows already handed out
    for this source in this run (the same per-source registry
    _youtube_start uses), so repeated picks of one long source walk
    its length instead of re-taking the opening window. Returns the
    window start in seconds, or None when every window of the file
    has been used. The caller stamps the segment
    "positional_no_contract" — the window is NEVER presented as
    transcript/chapter-verified evidence.
    """
    key = str(url or "").split("&", 1)[0].strip()
    if not key:
        return None
    try:
        total = float(source_duration)
        step = int(clip)
    except (TypeError, ValueError):
        return None
    if total <= 0 or step <= 0:
        return None
    max_start = max(0, int(total) - step)
    used = _yt_segment_uses.setdefault(key, [])
    candidate = 0
    while candidate <= max_start:
        end = candidate + step
        if not any(
            candidate < used_end and end > used_start
            for used_start, used_end in used
        ):
            used.append((candidate, end))
            return candidate
        candidate += step
    return None


def _segment_start_detailed(url, subject="", place="", event="", era="",
                            duration=DEFAULT_CLIP_DURATION,
                            provider_duration=None,
                            provider_duration_source=""):
    """Provider-neutral segment evidence (MS-3, extended VA-03/VA-06).

    Returns (start_seconds, evidence) or (None, "none"). Evidence
    classes:
    - "whole_asset": the source is short enough (<= 2x the clip length)
      that starting at 0 is the asset itself, not an arbitrary window
      of a long video. The duration may come from a remote probe,
      yt-dlp info, or — as a recorded fallback — the provider's own
      metadata (archive.org file lengths, Wikimedia videoinfo).
    - "verified_segment": the transcript/chapter/description evidence
      machinery (_youtube_start, yt-dlp based and NOT YouTube-only)
      selected a segment matched to the visual contract.
    - "positional_no_contract": an evenly spaced / deterministic
      positional window. For page URLs, _youtube_start chose it
      because the contract was empty; for a LONG DIRECT-MEDIA file
      (Gate 2), _positional_direct_window chose it because a direct
      file has no timed-evidence channel at all — the window is
      bounded by the probed or provider-supplied duration and
      fetched by download_direct_video's range-seek/trim. That is
      an estimate, NOT verified evidence, and is labelled
      accordingly (VA-06) in the segment basis, the attempts record
      and the accepted asset.
    - "none": no evidence — the caller must reject the segment rather
      than invent a timestamp. Timestamp zero is never assumed for a
      long source: a positional window is a labelled determination,
      and a long source whose duration is unknown, or whose windows
      are all used, still rejects.

    Every source tried is recorded in _SEGMENT_ATTEMPTS[url], and a
    failed lookup records its cause in _SEGMENT_FAILURE_CAUSE[url]
    ("info_unavailable" when the information channel itself failed,
    vs "no_evidence" when the duration was known but no timed
    evidence exists). A failed lookup NEVER becomes a segment.
    """
    if not url:
        return None, "none"
    clip = int(duration or DEFAULT_CLIP_DURATION)
    attempts = []
    _SEGMENT_ATTEMPTS[url] = attempts

    if is_direct_media_url(url):
        duration_source = ""
        source_duration = _remote_duration_seconds(url)
        if source_duration:
            duration_source = "remote_probe"
            _SOURCE_DURATION_CACHE[url] = float(source_duration)
            attempts.append({
                "source": "remote_probe", "outcome": "duration_known",
                "duration_seconds": float(source_duration),
            })
        else:
            attempts.append({"source": "remote_probe", "outcome": "unavailable"})
            if provider_duration:
                duration_source = "provider_metadata"
                _SOURCE_DURATION_CACHE[url] = float(provider_duration)
                attempts.append({
                    "source": "provider_metadata", "outcome": "duration_known",
                    "duration_seconds": float(provider_duration),
                    "provider": provider_duration_source,
                })
        source_duration = _SOURCE_DURATION_CACHE.get(url)
        if source_duration and float(source_duration) <= clip * 2:
            return 0, "whole_asset"
        if source_duration:
            # Gate 2: a long direct file has no timed-evidence
            # channel, and hard-rejecting it made most archive /
            # Wikimedia / institutional long-form footage unusable
            # (the #1 download-failure class of the Petito run).
            # It takes the labelled positional path instead: a
            # deterministic window bounded by the known duration,
            # stamped positional_no_contract everywhere the basis
            # is recorded — never upgraded to verified evidence.
            start = _positional_direct_window(
                url, float(source_duration), clip
            )
            if start is None:
                attempts.append({
                    "source": "positional_window",
                    "outcome": "windows_exhausted",
                    "duration_seconds": float(source_duration),
                    "duration_source": duration_source,
                })
                _SEGMENT_FAILURE_CAUSE[url] = "positional_windows_exhausted"
                return None, "none"
            attempts.append({
                "source": "positional_window",
                "outcome": "positional_no_contract",
                "duration_seconds": float(source_duration),
                "duration_source": duration_source,
                "window_start_seconds": int(start),
                "window_seconds": clip,
            })
            _SEGMENT_BASIS[
                str(url).split("&", 1)[0].strip()
            ] = "positional_no_contract"
            print(
                f"DIRECT SEGMENT (positional, no contract): {url} "
                f"start={int(start)}s end={int(start) + clip}s "
                f"source_duration={float(source_duration)}s "
                f"duration_source={duration_source}"
            )
            return int(start), "positional_no_contract"
        _SEGMENT_FAILURE_CAUSE[url] = "duration_unknown"
        return None, "none"

    # Page URLs on any yt-dlp-supported site: a short source qualifies
    # as a whole asset; longer sources need timed evidence.
    slim = _youtube_slim(str(url).split("&", 1)[0].strip())
    source_duration = None
    if slim and slim.get("duration"):
        try:
            source_duration = float(slim["duration"])
        except (TypeError, ValueError):
            source_duration = None
    if source_duration:
        _SOURCE_DURATION_CACHE[url] = float(source_duration)
        attempts.append({
            "source": "yt_dlp_info", "outcome": "duration_known",
            "duration_seconds": float(source_duration),
        })
    else:
        attempts.append({"source": "yt_dlp_info", "outcome": "unavailable"})
        if provider_duration:
            source_duration = float(provider_duration)
            _SOURCE_DURATION_CACHE[url] = source_duration
            attempts.append({
                "source": "provider_metadata", "outcome": "duration_known",
                "duration_seconds": source_duration,
                "provider": provider_duration_source,
            })
    if source_duration and source_duration <= clip * 2:
        return 0, "whole_asset"

    # Stage 7 / R8 (C.D.3 cost control): the segment index is built
    # only for sources that reached segment selection — they passed
    # the metadata gate and verification for at least one shot — and
    # only when the whole-asset shortcut did not already answer.
    _ensure_segment_index(url, slim)

    start = _youtube_start(
        url, subject=subject, place=place, event=event, era=era
    )
    basis = _SEGMENT_BASIS.get(str(url).split("&", 1)[0].strip())
    if start is None:
        attempts.append({"source": "timed_evidence", "outcome": "none_found"})
        _SEGMENT_FAILURE_CAUSE[url] = (
            "no_evidence" if source_duration else "info_unavailable"
        )
        return None, "none"
    attempts.append({
        "source": "timed_evidence", "outcome": basis or "verified_segment",
    })
    if basis == "positional_no_contract":
        return int(start), "positional_no_contract"
    return int(start), "verified_segment"


def _stamp_download_failure(candidate, reason, *, http_status=None,
                            detail=None):
    """Phase 3a / C-02: stamp the taxonomy record for a failed
    candidate download onto the candidate itself, beside the legacy
    ``_download_error`` string (kept for every existing consumer).
    The record — failure_class, http_status, verification check —
    is what _uve_fetch_first copies into the run's failure records
    and the asset report aggregates by class."""
    classified = download_taxonomy.classify_download_failure(
        reason,
        http_status=http_status,
        detail=detail,
        stage=candidate.get("rejection_stage"),
    )
    candidate["_download_failure"] = {
        "failure_class": classified["failure_class"],
        "reason": str(reason),
        "http_status": classified["http_status"],
        "check": classified["check"],
        "retryable": classified["retryable"],
        "provider": candidate.get("provider") or candidate.get("source") or "unknown",
        "url": str(
            candidate.get("url") or candidate.get("video_url")
            or candidate.get("image_url") or candidate.get("link") or ""
        ),
        "sentence_number": candidate.get("sentence_number"),
    }
    return classified


def _uve_download_video_candidate(
    candidate,
    scene_dir,
    sentence_number,
    asset_index,
):
    if not isinstance(candidate, dict):
        return None

    url = str(
        candidate.get("url")
        or candidate.get("video_url")
        or candidate.get("link")
        or ""
    ).strip()

    if not url:
        candidate["_download_error"] = "no_url"
        _stamp_download_failure(candidate, "no_url")
        return None

    need = candidate.get("shot_need") or {}

    subject = str(
        need.get("required_subject")
        or candidate.get("subject")
        or ""
    ).strip()

    place = str(
        need.get("place")
        or candidate.get("place")
        or ""
    ).strip()

    event = str(
        need.get("event")
        or candidate.get("event")
        or ""
    ).strip()

    era = str(
        need.get("era")
        or candidate.get("era")
        or ""
    ).strip()

    # MS-3: segment evidence is provider-neutral. ANY source — YouTube,
    # Vimeo, a news page, an archive file — must present evidence for
    # its segment: a whole-asset case (short source), a verified
    # timed segment, or — for a long direct-media file, which has no
    # timed-evidence channel — a labelled positional window (Gate 2,
    # stamped positional_no_contract on the asset). No evidence ->
    # reject; timestamp zero is never silently assumed for a long
    # video.
    provider_duration, provider_duration_source = (
        _provider_duration_seconds(candidate)
    )
    start, segment_evidence = _segment_start_detailed(
        url,
        subject=subject,
        place=place,
        event=event,
        era=era,
        duration=DEFAULT_CLIP_DURATION,
        provider_duration=provider_duration,
        provider_duration_source=provider_duration_source,
    )

    if start is None:
        print(
            f"SOURCE REJECTED (no segment evidence): "
            f"subject='{subject}' "
            f"place='{place}' "
            f"event='{event}' "
            f"era='{era}' "
            f"url={url}"
        )
        candidate["_download_error"] = "no_segment_evidence"
        _stamp_download_failure(candidate, "no_segment_evidence")
        # VA-03: preserve WHY evidence failed (broken information
        # channel vs genuinely absent evidence) and every source tried.
        candidate["_segment_attempts"] = list(_SEGMENT_ATTEMPTS.get(url) or [])
        candidate["_segment_failure_cause"] = (
            _SEGMENT_FAILURE_CAUSE.get(url) or ""
        )
        candidate["rejection_stage"] = "segment_evidence"
        return None

    # Stage 7 / R8 (C.D.3): extraction is cached BY SEGMENT within a
    # run. The extracted clip file is a function of (segment_id, clip
    # duration); the selection-time identity uses the nominal clip
    # duration (the probed identity is stamped after download). A
    # second beat needing the same segment reuses the extracted file
    # instead of re-downloading — the beat-relative fields (this
    # beat's verdict, candidate id, shot need) are re-stamped from
    # THIS candidate, because verdicts are contract-relative and
    # never inherited from another beat's decision.
    _selection_segment_id = segment_index.segment_id_for(
        canonical_source_key(url),
        float(start),
        float(start) + DEFAULT_CLIP_DURATION,
    )
    _cached_asset = _SEGMENT_FILE_CACHE.get(_selection_segment_id)
    if _cached_asset is not None and Path(
        _cached_asset.get("file_path") or ""
    ).exists():
        reused = dict(_cached_asset)
        for _field in (
            "verification", "candidate_id", "shot_need",
            "matched_query", "query", "universal_score",
            "relevance_score",
        ):
            if _field in candidate:
                reused[_field] = candidate[_field]
        reused["sentence_number"] = sentence_number
        reused["segment_reused"] = True
        candidate["segment_id"] = reused.get("segment_id")
        return reused

    # Phase 3 / AM-016: the failed-download guard at SEGMENT
    # granularity. The attempt is stamped with the segment it is
    # about to download; _uve_fetch_first records a failure under
    # "seg:<segment_id>" rather than the whole source, so one dead
    # window no longer poisons the source's other segments — a
    # genuinely dead source is still bounded, because every segment
    # attempt counts in the existing download-attempt accounting.
    # A segment that already failed this run is not re-downloaded.
    candidate["_attempted_segment_id"] = _selection_segment_id
    if f"seg:{_selection_segment_id}" in _FAILED_DOWNLOAD_URLS:
        candidate["_download_error"] = "previously_failed_this_run"
        _stamp_download_failure(candidate, "previously_failed_this_run")
        candidate["_segment_previously_failed"] = True
        return None

    safe_query = str(
        candidate.get("matched_query")
        or candidate.get("query")
        or "youtube_video"
    ).strip()

    safe_query = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        safe_query,
    ).strip("_")[:80] or "youtube_video"

    output_path = Path(scene_dir) / (
        f"sentence_{int(sentence_number):04d}"
        f"_video_{int(asset_index):02d}"
        f"_{int(start):05d}s"
        f"_{safe_query}.mp4"
    )

    try:
        ok = download_video_clip(
            url,
            output_path,
            start_time=start,
            duration=DEFAULT_CLIP_DURATION,
        )

    except Exception as exc:
        print(
            f"VIDEO DOWNLOAD FAILED: "
            f"{url} start={start}s error={exc}"
        )
        candidate["_download_error"] = f"download_exception: {exc}"
        _stamp_download_failure(
            candidate, f"download_exception: {exc}", detail=str(exc),
        )
        return None

    if not ok:
        candidate["_download_error"] = "download_failed"
        _last_error = DOWNLOAD_LAST_ERROR.pop(str(output_path), None)
        if _last_error:
            candidate["_download_failure"] = {
                "failure_class": _last_error["failure_class"],
                "reason": _last_error["reason"],
                "http_status": _last_error["http_status"],
                "check": _last_error["check"],
                "retryable": bool(_last_error.get("retryable")),
                "provider": candidate.get("provider") or candidate.get("source") or "unknown",
                "url": url,
                "detail": _last_error.get("detail"),
            }
        else:
            _stamp_download_failure(candidate, "download_failed")
        return None

    if not validate_video(output_path):
        candidate["_download_error"] = "invalid_media"
        _stamp_download_failure(candidate, "invalid_media")
        try:
            output_path.unlink()
        except OSError:
            pass

        return None

    # AUD-17: record the file's REAL probed facts. The asset previously
    # claimed duration_seconds = DEFAULT_CLIP_DURATION (8s) regardless of
    # what was actually downloaded.
    probe = probe_video(output_path)
    if probe is None:
        candidate["_download_error"] = "probe_failed"
        _stamp_download_failure(candidate, "probe_failed")
        try:
            output_path.unlink()
        except OSError:
            pass

        return None

    # VA-12: defensible technical minimums, recorded distinctly from
    # relevance rejections — the facts and the failed checks travel
    # with the candidate into the failure records and the report.
    technical_failures = _video_technical_failures(probe)
    if technical_failures:
        candidate["_download_error"] = "technical_below_minimum"
        _stamp_download_failure(
            candidate, "technical_below_minimum",
            detail=",".join(str(f) for f in technical_failures),
        )
        candidate["_technical_rejection"] = {
            "facts": probe,
            "failed": technical_failures,
            "minimums": _technical_minimums(),
        }
        candidate["rejection_stage"] = "technical_validation"
        try:
            output_path.unlink()
        except OSError:
            pass
        return None

    asset = dict(candidate)
    asset.pop("_download_error", None)
    _download_provenance = DOWNLOAD_PROVENANCE.pop(str(output_path), {}) or {}

    asset.update(
        {
            "file_path": str(output_path),
            "path": str(output_path),
            "video_url": url,
            "url": url,
            "type": "video",
            "visual_type": "video",
            "duration_seconds": probe["duration_seconds"],
            "technical_checks": {
                "passed": True,
                "facts": {
                    "width": probe["width"],
                    "height": probe["height"],
                    "fps": probe["fps"],
                    "codec": probe["codec"],
                    "has_audio": probe["has_audio"],
                },
                "minimums": _technical_minimums(),
            },
            "sentence_number": sentence_number,
            "clip_start": start,
            # MS-3: how the start time was evidenced — whole_asset,
            # verified_segment or positional_no_contract (Gate 2:
            # the labelled positional window of a long direct file);
            # recorded on every accepted video.
            "segment_evidence": segment_evidence,
            "segment_evidence_basis": (
                _SEGMENT_BASIS.get(str(url).split("&", 1)[0].strip())
                or segment_evidence
            ),
            # Segment-retrieval provenance: the ORIGINAL source
            # duration (when the evidence stage learned it), how the
            # clip was retrieved, and whether retrieval was genuinely
            # partial. Unknown stays None — never guessed.
            "source_duration_seconds": _SOURCE_DURATION_CACHE.get(url),
            "download_method": _download_provenance.get("method"),
            "partial_download": _download_provenance.get("partial"),
            "clip_duration": probe["duration_seconds"],
            "width": probe["width"],
            "height": probe["height"],
            "fps": probe["fps"],
            "codec": probe["codec"],
            "has_audio": probe["has_audio"],
            "probed": True,
            # AUD-15 rights evidence: who made it (when the provider says),
            # and when WE retrieved it. Licence stays whatever the
            # candidate honestly carried — "unknown" for YouTube.
            "creator": candidate.get("creator")
            or candidate.get("channel")
            or "",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    # Stage 7 / R8: mint the segment identity from the PROBED window
    # (the authoritative extracted segment) and stamp it on the asset
    # and the candidate; cache the extraction under the selection
    # identity so a later beat needing the same segment reuses this
    # file (the reuse branch above).
    asset["segment_id"] = segment_index.segment_id_for_asset(asset)
    candidate["segment_id"] = asset["segment_id"]
    if asset["segment_id"]:
        _SEGMENT_FILE_CACHE[_selection_segment_id] = dict(asset)

    print(
        f"YOUTUBE ASSET READY: "
        f"start={start}s "
        f"duration={probe['duration_seconds']}s "
        f"subject='{subject}' "
        f"place='{place}' "
        f"event='{event}' "
        f"era='{era}' "
        f"path={output_path}"
    )

    return asset


def _uve_download_image_candidate(
    candidate,
    scene_dir,
    sentence_number,
    asset_index,
):
    """Download one verified image candidate into a real image asset (AUD-13).

    Mirrors _uve_download_video_candidate: the file is downloaded with real
    content validation (download_valid_image decodes it via Pillow), then
    probed so the asset records true dimensions/format. Licence stays
    "unknown" unless the candidate itself carries evidence — it is never
    upgraded to a clearance it does not have.
    """
    if not isinstance(candidate, dict):
        return None

    image_url = str(
        candidate.get("image_url")
        or candidate.get("url")
        or ""
    ).strip()

    if not image_url:
        return None

    safe_query = str(
        candidate.get("matched_query")
        or candidate.get("query")
        or "image"
    ).strip()
    safe_query = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        safe_query,
    ).strip("_")[:80] or "image"

    url_path = image_url.split("?", 1)[0].lower()
    extension = ".jpg"
    for suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if url_path.endswith(suffix):
            extension = suffix
            break

    output_path = Path(scene_dir) / (
        f"sentence_{int(sentence_number):04d}"
        f"_image_{int(asset_index):02d}"
        f"_{safe_query}{extension}"
    )

    ok, reason = _download_image_detailed(image_url, output_path)
    if not ok:
        # VA-13: image failures carry explicit reason codes, exactly
        # like the video path — never a silent None.
        candidate["_download_error"] = reason
        _stamp_download_failure(candidate, reason)
        candidate["rejection_stage"] = (
            "technical_validation"
            if reason in ("undersized_file", "decode_failed", "probe_failed")
            else "download"
        )
        return None

    probe = probe_image(output_path)
    if probe is None:
        candidate["_download_error"] = "probe_failed"
        _stamp_download_failure(candidate, "probe_failed")
        candidate["rejection_stage"] = "technical_validation"
        try:
            output_path.unlink()
        except OSError:
            pass
        return None

    # VA-12: image technical minimums (thumbnail-grade stills cannot
    # serve a 1080p timeline).
    image_failures = _image_technical_failures(probe)
    if image_failures:
        candidate["_download_error"] = "technical_below_minimum"
        _stamp_download_failure(
            candidate, "technical_below_minimum",
            detail=",".join(str(f) for f in image_failures),
        )
        candidate["_technical_rejection"] = {
            "facts": probe,
            "failed": image_failures,
            "minimums": _technical_minimums(),
        }
        candidate["rejection_stage"] = "technical_validation"
        try:
            output_path.unlink()
        except OSError:
            pass
        return None

    page_url = str(
        candidate.get("source_url")
        or candidate.get("link")
        or ""
    ).strip()

    asset = dict(candidate)
    asset.update(
        {
            "file_path": str(output_path),
            "path": str(output_path),
            "image_url": image_url,
            "url": page_url or image_url,
            "source_url": page_url,
            "type": "image",
            "visual_type": "image",
            "sentence_number": sentence_number,
            "width": probe["width"],
            "height": probe["height"],
            "format": probe["format"],
            "probed": True,
            "technical_checks": {
                "passed": True,
                "facts": {"width": probe["width"], "height": probe["height"]},
                "minimums": _technical_minimums(),
            },
            "license": candidate.get("license") or "unknown",
            "rights_status": candidate.get("rights_status")
            or "verify_before_publish",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    # Stage 7 / R8: a still's segment identity is the whole-asset id
    # of its canonical source (C.D.3).
    asset["segment_id"] = segment_index.segment_id_for_asset(asset)
    candidate["segment_id"] = asset["segment_id"]

    print(
        f"IMAGE ASSET READY: "
        f"{probe['width']}x{probe['height']} "
        f"source={candidate.get('source') or ''} "
        f"path={output_path}"
    )

    return asset


def _download_url_key_for(candidate, is_image):
    """Canonical identity of the URL a download actually requests.

    For an image candidate that is the candidate's ``image_url``
    (the file), NOT the page ``url`` it was discovered on — the
    distinction the Gate 4 diagnosis (§6–§7) proved load-bearing:
    the same photograph is offered by many pages, and page-keyed
    memory can neither block its duplicate bytes nor remember its
    failed file URL. For a video candidate it is the source URL the
    video downloader itself resolves, in its own field precedence.
    """
    if not isinstance(candidate, dict):
        return ""
    if is_image:
        raw = (
            candidate.get("image_url")
            or candidate.get("url")
            or ""
        )
    else:
        raw = (
            candidate.get("url")
            or candidate.get("video_url")
            or candidate.get("link")
            or ""
        )
    raw = str(raw).strip()
    if not raw:
        return ""
    return canonical_source_key(raw) or raw.lower()


def _stamp_duplicate_content(candidate, digest, owner_sentence):
    """Record a cross-beat duplicate-content block on the candidate
    (Gate 4, diagnosis §6): the bytes are already committed to
    another beat this run. The taxonomy stamp is taken, then its
    class is set to ``duplicate`` — the class whose contract is
    exactly "already seen this run, never re-requested" — so the
    fetch loop's failure record tells the truth about why this
    pick produced no asset."""
    candidate["_download_error"] = "duplicate_content_this_run"
    _stamp_download_failure(candidate, "duplicate_content_this_run")
    if isinstance(candidate.get("_download_failure"), dict):
        candidate["_download_failure"]["failure_class"] = "duplicate"
    candidate["rejection_stage"] = "selection"
    candidate["duplicate_content"] = {
        "content_hash": digest,
        "first_sentence_number": owner_sentence,
    }


def _uve_download_any_candidate(candidate, scene_dir, sentence_number, asset_index):
    """F-2 dispatcher: route a unified-pool pick to its format's
    downloader. The pool kind was stamped when the pool was built.

    Gate 4 media seams (2026-11-26; diagnosis 08 §6–§7) — this
    dispatcher is the one seam every run download passes through
    (both _uve_fetch_first call sites pass it as the downloader),
    and the only one that sees the candidate, the beat's sentence
    number and the downloaded result together, so the per-run
    download memory lives here:

    1. Attempted-URL memory (§7): a download URL whose attempt
       failed TERMINALLY (the taxonomy's retryable flag is False —
       404 / source_unavailable / verification failures; transient
       classes such as timeouts, network errors and 429/5xx keep
       the retry policy they already had) is never re-attempted
       by a later pass, however the candidate is re-presented —
       a different source page for the same image file, or a
       different segment of the same dead video. A URL that
       succeeded resolves a same-beat re-presentation to the
       already-downloaded file (images; videos already resolve
       through the segment extraction cache).
    2. Cross-beat content block (§6): a still's content hash is
       known only once its bytes land, so the check sits here, at
       the commit point — bytes already committed to one beat are
       refused for a different beat (the fresh file is removed,
       the candidate is stamped duplicate_content_this_run) and
       the fetch loop moves on to the beat's next-best distinct
       candidate. Videos are exempt by design: their identity is
       the source window, never the container bytes (the
       visual_identity rule), and segment reuse across beats is
       legitimate. Within-beat re-presentation is untouched.
    """
    is_image = (
        isinstance(candidate, dict)
        and candidate.get("_pool_kind") == "image"
    )
    dl_key = _download_url_key_for(candidate, is_image)
    try:
        sentence_key = int(sentence_number)
    except (TypeError, ValueError):
        sentence_key = sentence_number

    if dl_key:
        prior_failure = _DOWNLOAD_URL_FAILURES.get(dl_key)
        if prior_failure is not None and not prior_failure.get("retryable"):
            candidate["_download_error"] = "previously_failed_this_run"
            _stamp_download_failure(
                candidate, "previously_failed_this_run"
            )
            candidate["rejection_stage"] = "download"
            candidate["_download_url_previously_failed"] = True
            return None
        if is_image:
            cached = _DOWNLOADED_ASSETS_BY_URL.get(dl_key)
            if cached is not None and Path(
                cached.get("file_path") or ""
            ).exists():
                if cached.get("sentence_number") == sentence_key:
                    # Same beat re-presenting a URL that already
                    # downloaded: resolve to the downloaded file
                    # instead of downloading again — the Stage 7
                    # segment-reuse shape, with this presentation's
                    # beat-relative fields re-stamped from THIS
                    # candidate (verdicts are never inherited).
                    reused = dict(cached)
                    for _field in (
                        "verification", "candidate_id", "shot_need",
                        "matched_query", "query", "universal_score",
                        "relevance_score",
                    ):
                        if _field in candidate:
                            reused[_field] = candidate[_field]
                    reused["sentence_number"] = sentence_number
                    reused["segment_reused"] = True
                    reused["download_reused"] = True
                    candidate["segment_id"] = reused.get("segment_id")
                    return reused
                # A different beat asking for the very URL whose
                # bytes are committed elsewhere: identical content
                # by construction — blocked without any download.
                _stamp_duplicate_content(
                    candidate,
                    cached.get("content_hash"),
                    cached.get("sentence_number"),
                )
                return None

    if is_image:
        asset = _uve_download_image_candidate(
            candidate, scene_dir, sentence_number, asset_index
        )
    else:
        asset = _uve_download_video_candidate(
            candidate, scene_dir, sentence_number, asset_index
        )

    if asset is None:
        # Record the attempt's outcome under the DOWNLOAD URL's
        # identity. Images: every failure here follows a real
        # attempt of that URL. Videos: only a stamped segment
        # attempt proves the URL itself was requested — a
        # no-segment-evidence refusal never touched the download
        # and keeps its existing (segment/source) accounting.
        if dl_key and isinstance(candidate, dict):
            stamp = candidate.get("_download_failure")
            attempted = is_image or bool(
                candidate.get("_attempted_segment_id")
            )
            if (
                attempted
                and isinstance(stamp, dict)
                and stamp.get("failure_class")
            ):
                _DOWNLOAD_URL_FAILURES[dl_key] = {
                    "retryable": bool(stamp.get("retryable")),
                    "failure_class": stamp.get("failure_class"),
                    "reason": stamp.get("reason"),
                }
                # Phase 2 write-through: the failure also enters
                # the production's durable discovery memory, so the
                # NEXT run's dispatcher refuses it from the seeded
                # guard without any attempt (terminal classes), and
                # transient classes stay retryable only until the
                # store's stated TTL expires them.
                _store = _active_discovery_memory()
                if _store is not None:
                    try:
                        _store.record_download_failure(
                            "download_url", dl_key,
                            failure_class=stamp.get("failure_class"),
                            retryable=bool(stamp.get("retryable")),
                            reason=stamp.get("reason"),
                        )
                    except Exception:
                        pass
        return None

    if dl_key:
        # A success supersedes any earlier (transient) failure
        # recorded for the URL in this run.
        _DOWNLOAD_URL_FAILURES.pop(dl_key, None)
        _store = _active_discovery_memory()
        if _store is not None:
            try:
                _store.record_download_success(
                    dl_key,
                    content_hash=asset.get("content_hash"),
                    sentence_number=(
                        sentence_key
                        if isinstance(sentence_key, int) else None
                    ),
                )
            except Exception:
                pass

    if is_image:
        digest = asset.get("content_hash") or _uve_content_hash(
            asset.get("file_path") or asset.get("path")
        )
        if digest:
            if not asset.get("content_hash"):
                asset["content_hash"] = digest
            owner = _COMMITTED_CONTENT_OWNERS.get(digest)
            if owner is not None and owner != sentence_key:
                # Cross-beat duplicate content: these exact bytes
                # are already committed to another beat. Remove the
                # fresh file (it is this download's own, never a
                # shared one), stamp the block, and let the fetch
                # loop take the beat's next-best distinct pick.
                try:
                    dup_path = asset.get("file_path")
                    if dup_path and os.path.exists(dup_path):
                        os.remove(dup_path)
                except OSError:
                    pass
                _stamp_duplicate_content(candidate, digest, owner)
                return None
            _COMMITTED_CONTENT_OWNERS.setdefault(digest, sentence_key)
        if dl_key:
            _DOWNLOADED_ASSETS_BY_URL[dl_key] = dict(asset)

    return asset


def _uve_search_and_pick(
    scenario, shot_need, *,
    search_cache, project_id, bible, registry, used_keys,
    scene_dir, scene_stats, beat, beat_videos, beat_images,
    scenario_index, queries, review_records,
    query_pass="primary",
    phase="full",
):
    """One full discovery pass for a scenario.

    Search every enabled provider, verify every candidate, rank the
    survivors in the unified format-aware pool, pick under the
    verifier's acceptance rules and download the winner. All accounting
    (provider funnel, candidate totals, rejection reasons, download
    failures, discovery failures, review record) is recorded on
    scene_stats / review_records. An accepted asset is appended to
    beat_videos / beat_images and returned; otherwise None is returned
    and the caller decides about coverage recovery. Nothing here ever
    fabricates or substitutes an asset.

    Coverage pass: when the beat's fetched assets (this pass's winner
    plus any from earlier scenarios of the beat) cannot cover the
    beat's sentences under the timeline's segment-use cap, further
    verifier-accepted pool candidates are drawn in selection-rank
    order (see the COVERAGE PASS block below) and likewise appended;
    the first coverage asset is returned when the primary fetch
    produced none.

    Phase 2 (Discovery Efficiency) — the fair initial pass. The pass
    runs in two phases: the INITIAL phase (search, verify, pool,
    primary fetch, and the coverage draw over this pass's OWN
    verified pool) and the EXTENDED phase (tier-ladder escalation,
    the variety draw, and the candidate-record/review accounting),
    which lives in _uve_search_and_pick_extended and consumes the
    state dict the initial phase assembles. With phase="full" (the
    default — every historical caller) both phases run back to back
    and the behaviour is exactly the pre-split pass. With
    phase="initial" the function returns (primary_asset, state) and
    the caller MUST later run _uve_search_and_pick_extended(state):
    the scene driver does this for every beat of a scene only after
    EVERY beat has had its initial phase, so no sentence's extended
    effort can precede another sentence's first search (Phase 0 root
    cause 3: Hyatt S13/S14 absorbed 527 evaluations each while
    S20/S21 were never searched).
    """
    _query_log = []
    _QUERY_LOG_STACK.append(_query_log)
    try:
        raw_videos, raw_web_sources, raw_images = _uve_scenario_search(
            scenario, search_cache, project_id, shot_need,
        )
    finally:
        _QUERY_LOG_STACK.pop()
    scored_videos = _uve_verify_all(
        raw_videos,
        scenario,
        bible,
        shot_need=shot_need,
    )
    scored_web_sources = _uve_verify_all(
        raw_web_sources,
        scenario,
        bible,
        shot_need=shot_need,
    )
    scored_images = _uve_verify_all(
        raw_images,
        scenario,
        bible,
        shot_need=shot_need,
    )
    print(f"      candidates: videos={len(scored_videos)} web={len(scored_web_sources)} images={len(scored_images)}")

    # F-4/MS-6: provider + kind funnels for the asset
    # report. Discovered = raw pool size; verified/rejected
    # from the scored lists (the verifier's own verdicts).
    _provider_funnel = scene_stats.setdefault("providers", {})
    _kind_funnel = scene_stats.setdefault("kinds", {})

    def _funnel_add(pool, scored, kind):
        kind_counts = _kind_funnel.setdefault(
            kind,
            {"discovered": 0, "verified": 0, "rejected": 0, "accepted": 0},
        )
        kind_counts["discovered"] += len(pool)
        for cand in pool:
            provider = str(
                cand.get("provider") or cand.get("source") or "unknown"
            )
            _provider_funnel.setdefault(
                provider,
                {"discovered": 0, "verified": 0, "rejected": 0, "downloaded": 0, "download_failed": 0},
            )["discovered"] += 1
        for cand in scored:
            provider = str(
                cand.get("provider") or cand.get("source") or "unknown"
            )
            entry = _provider_funnel.setdefault(
                provider,
                {"discovered": 0, "verified": 0, "rejected": 0, "downloaded": 0, "download_failed": 0},
            )
            if (cand.get("verification") or {}).get("accepted"):
                entry["verified"] += 1
                kind_counts["verified"] += 1
            else:
                entry["rejected"] += 1
                kind_counts["rejected"] += 1

    _funnel_add(raw_videos, scored_videos, "video")
    _funnel_add(raw_web_sources, scored_web_sources, "web")
    _funnel_add(raw_images, scored_images, "image")

    # Stage 0 metrics: candidates discovered in this pass — the same
    # raw pools the funnels above are built from.
    run_metrics.candidates_discovered(
        len(raw_videos) + len(raw_web_sources) + len(raw_images)
    )

    # Stage 1 counter fix: web sources were counted in
    # candidates_rejected (below) and in every funnel but EXCLUDED
    # here, so the asset report could show rejected > considered and
    # every verified web candidate was invisible in the considered
    # total. All three scored pools are considered.
    scene_stats["candidates_considered"] += (
        len(scored_videos) + len(scored_web_sources) + len(scored_images)
    )
    scene_stats["candidates_rejected"] += sum(
        1
        for item in (scored_videos + scored_web_sources + scored_images)
        if item.get("reject_reason")
    )

    # Phase 3 / AM-021: per-query diagnostics for THIS pass — every
    # query actually issued (from the search's own log), with its
    # source, its raw hits, and how its candidates fared: scored
    # candidates carrying its matched_query, and how many of those
    # the verifier accepted. Recorded per pass so "are our queries
    # bad?" is answerable per beat from the asset report. (Tier
    # ladder searches are accounted separately, in tier_attempts.)
    _scored_all = scored_videos + scored_web_sources + scored_images
    _query_diagnostics = []
    for _entry in _query_log:
        _q = _entry["query"]
        _q_candidates = [
            c for c in _scored_all
            if str(c.get("matched_query") or "") == _q
        ]
        _query_diagnostics.append({
            "query": _q,
            "source": _entry["source"],
            "hits": _entry["hits"],
            "candidates": len(_q_candidates),
            "verified": sum(
                1 for c in _q_candidates
                if (c.get("verification") or {}).get("accepted")
            ),
        })
    scene_stats.setdefault("query_diagnostics", []).append({
        "beat_id": (beat or {}).get("beat_id"),
        "shot_index": scenario_index,
        "pass": query_pass,
        "tier": str((shot_need or {}).get("tier") or "direct"),
        "queries": _query_diagnostics,
    })

    # Failure classification is NO LONGER counted here. The old
    # parallel increment (with its divergent score-below alias
    # fallback string) could and did drift from the candidate
    # records; Stage 6 / C.A.3.4 rebuilds scene_stats
    # ["rejection_reasons"] FROM the records at the end of this pass
    # (see below), so the records are the single counting source.

    # F-2: UNIFIED SELECTION. Verified video candidates,
    # footage-capable web candidates (MS-2) and verified image
    # candidates compete in ONE pool for this scenario. Ranking
    # is relevance (universal_score) adjusted by format-intent
    # fit — a bounded bonus (max 0.10) that can tip a close
    # contest toward the format that best communicates the
    # sentence, but can never rescue a weak candidate over a
    # clearly more relevant one of another format, and never
    # admits a verifier-rejected candidate (_uve_pick still
    # enforces acceptance). No format is tried "first" any
    # more: the old video->image cascade is gone.
    intent = str(scenario.get("format_intent") or "any").lower()
    intent_conf = scenario.get("format_intent_confidence") or 0.0
    try:
        intent_conf = float(intent_conf)
    except (TypeError, ValueError):
        intent_conf = 0.0

    def _intent_bonus(kind):
        if intent == "any" or intent_conf <= 0:
            return 0.0
        if kind == "video":
            return 0.10 * intent_conf if intent == "footage" else 0.0
        # image-side candidates serve photograph/document/
        # map/chart intents fully, graphic intents partially.
        if intent in {"photograph", "document", "map", "chart"}:
            return 0.10 * intent_conf
        if intent == "graphic":
            return 0.05 * intent_conf
        return 0.0

    def _rank_stamp(_item, _kind):
        """Phase 3 ranking stamp: selection score = universal score
        + format-intent bonus + the bounded intent adjustment from
        _intent_rank_adjustment (medium fit / misrepresentation
        demotion), with the adjustment and its reasons recorded on
        the item. Ranking only — the verifier already decided
        eligibility; this never changes a verdict."""
        _bonus = _intent_bonus(_kind)
        _adjustment, _reasons = _intent_rank_adjustment(
            _item, _kind, shot_need, scenario, _bonus
        )
        _item["_intent_adjustment"] = _adjustment
        if _reasons:
            _item["_intent_reasons"] = _reasons
        _item["_selection_score"] = (
            (_item.get("universal_score") or 0) + _bonus + _adjustment
        )
        return _item

    combined_pool = []
    pool_keys = set()
    for item in scored_videos:
        item = dict(item)
        item["_pool_kind"] = "video"
        _rank_stamp(item, "video")
        combined_pool.append(item)
        pool_keys.add(_uve_candidate_key(item))
    for web_item in scored_web_sources:
        if not _web_source_is_video_candidate(web_item):
            continue
        web_key = _uve_candidate_key(web_item)
        if not web_key or web_key in pool_keys:
            continue
        pool_keys.add(web_key)
        web_item = dict(web_item)
        web_item["_pool_kind"] = "video"
        _rank_stamp(web_item, "video")
        combined_pool.append(web_item)
    for item in scored_images:
        item = dict(item)
        item["_pool_kind"] = "image"
        _rank_stamp(item, "image")
        combined_pool.append(item)

    combined_pool.sort(key=lambda c: c.get("_selection_score") or 0, reverse=True)

    picks = _uve_pick(combined_pool, scenario, registry, used_keys, shot_need=shot_need)
    fetch_failures = []
    asset = _uve_fetch_first(
        picks, _uve_download_any_candidate, scene_dir,
        beat["sentence_numbers"][0], len(beat_videos) + len(beat_images) + 1,
        registry, used_keys, failures=fetch_failures,
        transcript_scenario=scenario, transcript_bible=bible,
    )
    if fetch_failures:
        scene_stats.setdefault("download_failures", []).extend(fetch_failures)
        for failure in fetch_failures:
            entry = scene_stats.setdefault("providers", {}).setdefault(
                str(failure.get("provider") or "unknown"),
                {"discovered": 0, "verified": 0, "rejected": 0, "downloaded": 0, "download_failed": 0},
            )
            entry["download_failed"] += 1
    accepted_asset = None
    if asset:
        _dl_provider = str(asset.get("provider") or asset.get("source") or "unknown")
        scene_stats.setdefault("providers", {}).setdefault(
            _dl_provider,
            {"discovered": 0, "verified": 0, "rejected": 0, "downloaded": 0, "download_failed": 0},
        )["downloaded"] += 1
        annotated = _uve_annotate(asset, scenario, scenario_index, beat.get("text", ""))
        # B-roll B0 (§6.1): the accepted asset record carries its
        # tier through the scene's sentence results — the need's
        # tier ("direct" for every search run today).
        annotated["tier"] = str((shot_need or {}).get("tier") or "direct")
        accepted_asset = annotated
        # Phase 3 / AM-004: the per-kind funnel gains its final stage
        # — accepted — counted by the winning candidate's pool kind
        # (a footage-capable web candidate counts as video, the kind
        # it competed as), so the video preference's effect is
        # measurable: discovered -> verified -> accepted by kind.
        _accepted_kind = asset.get("_pool_kind") or (
            "video"
            if (asset.get("type") == "video" or asset.get("visual_type") == "video")
            else "image"
        )
        _kind_funnel.setdefault(
            _accepted_kind,
            {"discovered": 0, "verified": 0, "rejected": 0, "accepted": 0},
        )["accepted"] = _kind_funnel[_accepted_kind].get("accepted", 0) + 1
        if asset.get("type") == "video" or asset.get("visual_type") == "video":
            beat_videos.append(annotated)
            print(f"      + video ({asset.get('verification_status')}, score {asset.get('universal_score')})")
        else:
            beat_images.append(annotated)
            print(f"      + image ({asset.get('verification_status')}, score {asset.get('universal_score')})")

    # ------------------------------------------------------------
    # COVERAGE PASS (founder-approved utilisation fix, 2026-11-21;
    # founder live test 2): the pick limit is a preference, not a
    # starvation cause. When the beat's fetched assets cannot cover
    # all of the beat's sentences under the timeline's segment-use
    # cap, draw further VERIFIER-ACCEPTED candidates from THIS
    # pass's own pool — the ones _uve_pick stamped
    # pick_limit_reached — in selection-rank order, until coverage
    # is possible or the pool is genuinely exhausted. Only
    # candidates the verifier accepted can be pool members at all
    # (AUD-16: no relaxed pool, ever); each draw re-runs the pick's
    # own eligibility checks, and a drawn candidate's
    # pick_limit_reached stamp is cleared before its fetch, so its
    # record shows the truth — coverage-picked, then accepted or
    # download_failed — never left standing as rejected.
    # ------------------------------------------------------------
    coverage_accepted_assets = []
    variety_accepted_assets = []
    _sentence_count = len((beat or {}).get("sentence_numbers") or [])
    _shortfall = _uve_coverage_shortfall(
        _sentence_count,
        len(_uve_beat_distinct_asset_keys(beat_videos, beat_images)),
    )

    def _pool_annotate(_items, _kind):
        """Mirror of the combined_pool annotation in the assembly
        above: pool copies carrying their pool kind and their
        intent-adjusted selection score. Web sources join as video
        candidates only when footage-capable — the same rule the
        main assembly applies."""
        _out = []
        for _item in _items:
            if _kind == "web" and not _web_source_is_video_candidate(_item):
                continue
            _copy = dict(_item)
            _copy["_pool_kind"] = "video" if _kind == "web" else _kind
            _rank_stamp(_copy, "image" if _kind == "image" else "video")
            _out.append(_copy)
        return _out

    # ------------------------------------------------------------------
    # Tier ladder helpers (Intelligent B-Roll plan §5; Master
    # Directive Phase 2 / B2 + D-B3).
    #
    # _escalate_pool runs ONE need's escalation: derived scenarios
    # built from the need's fallback concepts + visual intentions
    # (services.tier_ladder) are normalised through the ordinary
    # plan machinery and searched / verified through the SAME
    # _uve_scenario_search / _uve_verify_all path as any direct
    # need — the Phase 1 eligibility gate and verifier are not
    # bypassed, relaxed or duplicated. Tiers run in ladder order
    # and stop at the first tier that yields a verified candidate.
    # D-B2 budgets bind throughout: per-need query budgets per tier,
    # and the production ledger's extra-search / download /
    # wall-clock accounts. Every tier's outcome is recorded in the
    # returned attempts list — budget exhaustion is data, never a
    # silent stop.
    # Breadth-first allocation (tier_ladder): this pass's need key.
    # Stable across the beat's primary pass and its coverage
    # recovery rounds (same beat dict, same scenario index, same
    # first sentence), unique across scenes (sentence numbers are
    # production-unique), and identical to the key the unsearched
    # entry registers for its own beat — so a beat's T3 entry and
    # its T4 continuation inside this pass share one account.
    _escalation_need_key = (
        (shot_need or {}).get("beat_id") or (beat or {}).get("beat_id"),
        scenario_index,
        ((beat or {}).get("sentence_numbers") or [None])[0],
    )

    def _escalate_pool(_need):
        _attempts = []
        _pool_items = []
        _scored_by_kind = {"video": [], "web": [], "image": []}
        if not isinstance(_need, dict) or not _need:
            return _pool_items, _attempts, _scored_by_kind
        _ledger = tier_ladder.ledger_for(registry)
        _ledger.register_need(_escalation_need_key)
        _ledger.begin_escalation()
        _pack_id, _vocabulary = tier_ladder.broll_vocabulary_for(bible)
        _owner_sentences = (beat or {}).get("sentences") or []
        _owner_sentence = (
            _owner_sentences[0]
            if _owner_sentences and isinstance(_owner_sentences[0], dict)
            else {}
        )
        _owner_text = get_sentence_text(_owner_sentence)
        for _tier in tier_ladder.tiers_after(_need.get("tier")):
            if _pool_items:
                break
            _attempt = {
                "tier": _tier,
                "queries": [],
                "verified": 0,
                "outcome": None,
                "stop_reason": None,
            }
            _attempts.append(_attempt)
            if _ledger.wall_clock_exceeded():
                _attempt["outcome"] = "budget_exhausted"
                _attempt["stop_reason"] = tier_ladder.STOP_WALL_CLOCK
                continue
            if _tier == "atmospheric":
                # UV-05 graphic-preferred routing: the walk only
                # reaches the atmospheric tier with zero verified
                # candidates from every earlier tier (the loop
                # breaks on a non-empty pool). When the need's own
                # plan data marks the beat as inherently
                # explanatory, generic atmospheric footage is the
                # weakest support — weaker than the designed
                # graphic the graphics path produces — so the
                # atmospheric spend is SKIPPED (no queries, no
                # downloads, no ledger charge) and the beat routes
                # to graphics: the attempt records the signal, the
                # pool stays empty, and the scene driver stamps
                # the beat's missing_visuals entries
                # "graphic_preferred" (jobs builds graphic targets
                # from missing_visuals regardless of reason).
                _gp_signal = tier_ladder.graphic_preferred_signal(
                    _need, scenario,
                )
                if _gp_signal is not None:
                    _attempt["outcome"] = tier_ladder.GRAPHICS_PREFERRED
                    _attempt["graphic_preferred"] = _gp_signal
                    print(
                        "  GRAPHIC-PREFERRED: atmospheric tier "
                        f"skipped (signal={_gp_signal['signal']}, "
                        f"kind={_gp_signal.get('kind')}) — beat "
                        "routes to the graphics path"
                    )
                    break
            _raw_scenarios = tier_ladder.derived_scenarios_for_tier(
                _need, bible, _tier, vocabulary=_vocabulary,
            )
            if not _raw_scenarios:
                _attempt["outcome"] = "no_concepts"
                continue
            _query_budget = tier_ladder.TIER_QUERY_BUDGETS.get(_tier, 0)
            _tier_pool = []
            _repeated_batches = 0
            for _raw in _raw_scenarios:
                if len(_attempt["queries"]) >= _query_budget:
                    break
                _plan = story.normalize_plan({
                    "text": _owner_text,
                    "role": (
                        _owner_sentence.get("role")
                        or (beat or {}).get("role")
                        or "background"
                    ),
                    "subject_id": _raw.get("subject_id"),
                    "place": _raw.get("place") or _need.get("place"),
                    "time": _raw.get("era") or _need.get("era"),
                    "scenarios": [_raw],
                    "fallback_concepts": _need.get("fallback_concepts"),
                    "visual_intents": _need.get("visual_intents"),
                }, _owner_text, bible, None)
                # The pipeline reads a beat's scenarios from its
                # sentence records (_beat_scenarios), so the
                # derived scenario rides on a synthetic sentence
                # record carrying the parent need's concepts and
                # intentions — the same shape the analyzer produces.
                _dbeat = {
                    "beat_id": (beat or {}).get("beat_id"),
                    "searchable": True,
                    "sentences": [{
                        "text": _owner_text,
                        "role": _owner_sentence.get("role"),
                        "scenarios": _plan["scenarios"],
                        "fallback_concepts": _need.get("fallback_concepts"),
                        "concepts_source": _need.get("concepts_source"),
                        "visual_intents": _need.get("visual_intents"),
                    }],
                    "sentence_numbers": (
                        (beat or {}).get("sentence_numbers") or []
                    ),
                    "subject_id": (beat or {}).get("subject_id"),
                    "place": (beat or {}).get("place"),
                }
                _dneeds = build_shot_needs(_dbeat, bible)
                if not _dneeds:
                    continue
                _dneed = _dneeds[0]
                _dneed["tier"] = _tier
                _dneed["derived_from"] = {
                    "shot_index": scenario_index,
                    "parent_tier": str(_need.get("tier") or "direct"),
                }
                _dneed["vocabulary_pack"] = _pack_id
                _dscenario = (
                    _beat_scenarios(_dbeat) or _plan["scenarios"]
                )[0]
                _remaining = _query_budget - len(_attempt["queries"])
                _queries = list(_dneed.get("queries") or [])[:_remaining]
                if not _queries:
                    continue
                # Attempt idempotency (run-10 fix): this exact
                # batch — same need scope (this beat), same tier,
                # same normalised query set — already completed in
                # this production (a sibling shot's walk or an
                # earlier recovery round's walk of THIS beat). Its
                # results were already verified and considered;
                # re-searching would only re-spend the shared D-B2
                # account. The batch is NOT re-executed: a verified
                # batch's recorded pool replays into this walk's
                # pool (the draw below may still fetch a candidate
                # that was never fetched — the draw's own budgets
                # govern that), and an exhausted batch contributes
                # nothing. Recovery rounds therefore spend only on
                # NEW queries and tiers. A DIFFERENT beat deriving
                # identical queries is a different batch: fairness
                # gives every distinct need its own first attempts.
                # Identity is the scenario's FULL composed query
                # list: the slices above it (tier budget remaining,
                # fairness allocation) vary with walk state — in
                # particular a walk whose earlier batches were
                # repeats has more tier budget left, so a sliced
                # identity would drift between walks and the same
                # batch would re-execute under a wider slice.
                _batch_queries = list(_dneed.get("queries") or [])
                _prior_attempt = _ledger.completed_attempt(
                    _tier, _batch_queries,
                    need_key=_escalation_need_key,
                )
                if _prior_attempt is not None:
                    _repeated_batches += 1
                    # Replay only what is still fetchable: a
                    # candidate whose source (or segment) already
                    # failed a fetch in this run is dead by the
                    # Phase-2 guard inside _uve_fetch_first — it
                    # would be skipped there while still burning
                    # one of the draw's per-tier attempts, exactly
                    # the waste this fix exists to remove. (Run
                    # 10's pass 17 only reached its third verified
                    # candidate because the fresh pool re-ordered;
                    # a replay must not depend on ordering luck.)
                    for _replay_item in (
                        _prior_attempt.get("pool_items") or []
                    ):
                        _replay_url = str(
                            _replay_item.get("url")
                            or _replay_item.get("video_url")
                            or _replay_item.get("image_url")
                            or ""
                        )
                        if _replay_url and (
                            canonical_source_key(_replay_url)
                            in _FAILED_DOWNLOAD_URLS
                        ):
                            continue
                        _replay_seg = _replay_item.get("segment_id")
                        if _replay_seg and (
                            f"seg:{_replay_seg}"
                            in _FAILED_DOWNLOAD_URLS
                        ):
                            continue
                        _tier_pool.append(_replay_item)
                    continue
                # Breadth-first allocation: the ledger may grant
                # fewer searches than the batch asks for — a need's
                # protected first query always runs while budget
                # remains, but deepening stops at the fairness
                # reserve. Only the granted queries execute, and
                # only they are recorded on the attempt.
                _allowed = _ledger.allocate_searches(
                    len(_queries), need_key=_escalation_need_key,
                )
                if _allowed <= 0:
                    _attempt["stop_reason"] = (
                        tier_ladder.STOP_WALL_CLOCK
                        if _ledger.wall_clock_exceeded()
                        else tier_ladder.STOP_PRODUCTION_SEARCH_BUDGET
                    )
                    # Run-11 fix 4: the budget stop ends this
                    # tier's DEEPENING — it does not rewrite what
                    # the tier already did. When an earlier batch
                    # of this same walk verified candidates into
                    # the tier pool (run 11 sentence 12: "phone
                    # 2021" verified 5, then the next batch's
                    # allocation hit the wall clock and the attempt
                    # was recorded budget_exhausted despite the
                    # verified pool), the outcome stays the pool's
                    # truth — the post-loop computation records
                    # "verified" — and only the stop_reason names
                    # the budget cause. budget_exhausted is
                    # recorded only when the tier verified nothing.
                    if not any(
                        (_item.get("verification") or {}).get("accepted")
                        for _item in _tier_pool
                    ):
                        _attempt["outcome"] = "budget_exhausted"
                    break
                _queries = _queries[:_allowed]
                _dneed["queries"] = _queries
                _dscenario["queries"] = _queries
                _ledger.spend_searches(
                    len(_queries), need_key=_escalation_need_key,
                )
                _attempt["queries"].extend(_queries)
                _raw_v, _raw_w, _raw_i = _uve_scenario_search(
                    _dscenario, search_cache, project_id, _dneed,
                )
                _scored_v = _uve_verify_all(
                    _raw_v, _dscenario, bible, shot_need=_dneed,
                )
                _scored_w = _uve_verify_all(
                    _raw_w, _dscenario, bible, shot_need=_dneed,
                )
                _scored_i = _uve_verify_all(
                    _raw_i, _dscenario, bible, shot_need=_dneed,
                )
                for _scored in (_scored_v + _scored_w + _scored_i):
                    # The candidate's own tier stamp: its event
                    # records (built below from the scored lists)
                    # carry the tier it was evaluated under.
                    _scored["tier"] = _tier
                _funnel_add(_raw_v, _scored_v, "video")
                _funnel_add(_raw_w, _scored_w, "web")
                _funnel_add(_raw_i, _scored_i, "image")
                run_metrics.candidates_discovered(
                    len(_raw_v) + len(_raw_w) + len(_raw_i)
                )
                scene_stats["candidates_considered"] += (
                    len(_scored_v) + len(_scored_w) + len(_scored_i)
                )
                scene_stats["candidates_rejected"] += sum(
                    1
                    for _cand in (_scored_v + _scored_w + _scored_i)
                    if _cand.get("reject_reason")
                )
                _scored_by_kind["video"].extend(_scored_v)
                _scored_by_kind["web"].extend(_scored_w)
                _scored_by_kind["image"].extend(_scored_i)
                _pool_before = len(_tier_pool)
                _tier_pool.extend(_pool_annotate(_scored_v, "video"))
                _tier_pool.extend(_pool_annotate(_scored_w, "web"))
                _tier_pool.extend(_pool_annotate(_scored_i, "image"))
                # Attempt idempotency (run-10 fix): record this
                # executed batch as terminal — verified with its
                # pool (for replay), or pool_exhausted — so no
                # later walk in this production re-spends on it.
                _batch_pool = _tier_pool[_pool_before:]
                _batch_verified = sum(
                    1
                    for _item in _batch_pool
                    if (_item.get("verification") or {}).get("accepted")
                )
                _ledger.record_attempt(
                    _tier, _batch_queries,
                    outcome=(
                        "verified" if _batch_verified
                        else "pool_exhausted"
                    ),
                    verified=_batch_verified,
                    pool_items=[
                        _item
                        for _item in _batch_pool
                        if (_item.get("verification") or {}).get(
                            "accepted"
                        )
                    ],
                    need_key=_escalation_need_key,
                )
            # One copy per candidate within the tier (two derived
            # scenarios can surface the same source; cross-pool
            # duplicates with the T1 pool are the draw's business —
            # its eligibility re-check refuses a second fetch).
            _seen_keys = set()
            _deduped = []
            for _item in _tier_pool:
                _key = _uve_candidate_key(_item)
                if _key and _key in _seen_keys:
                    continue
                if _key:
                    _seen_keys.add(_key)
                _deduped.append(_item)
            _tier_pool = _deduped
            _tier_pool.sort(
                key=lambda c: c.get("_selection_score") or 0,
                reverse=True,
            )
            for _rank, _item in enumerate(_tier_pool):
                # Escalation candidates enter the draw as reserves,
                # exactly like pick-limit reserves: the stamp is
                # cleared by the draw when one is consumed, and
                # stands (truthfully) on the ones never drawn.
                _item["tier_escalation"] = _tier
                _item["rejection_stage"] = "selection"
                _item["reject_reason"] = (
                    rejection_diagnostics.PICK_LIMIT_REASON
                )
                _item["selection_rank"] = _rank
            _attempt["verified"] = sum(
                1
                for _item in _tier_pool
                if (_item.get("verification") or {}).get("accepted")
            )
            if _attempt["outcome"] is None:
                _attempt["outcome"] = (
                    "verified" if _attempt["verified"] else "pool_exhausted"
                )
            if _repeated_batches:
                # Attempt idempotency (run-10 fix): batches this
                # walk recognised as repeats of an earlier completed
                # batch — they executed nothing and spent nothing.
                # A walk whose batches ALL repeated inherits the
                # recorded outcome verbatim (the verified count
                # above already reflects any replayed pool), and is
                # stamped as a repeat so the report never mistakes
                # it for a fresh walk.
                _attempt["repeated_batches"] = _repeated_batches
                if not _attempt["queries"]:
                    _attempt["repeat"] = True
            # The walk continues until a tier yields a VERIFIED
            # candidate: a tier whose pool is all rejections (its
            # candidates' events are already recorded from the
            # scored lists) does not stop the ladder.
            if _attempt["verified"]:
                _pool_items = _tier_pool
        _ledger.end_escalation()
        return _pool_items, _attempts, _scored_by_kind

    def _draw_reserves(_items, _shortfall, _drawn_ids, _blocked,
                       _failures, _via="coverage_pass", _need_fn=None,
                       _sink=None, _budget_blocked=None,
                       _draw_stats=None):
        """One coverage draw over reserve pool items: eligibility
        re-checked per item at draw time, the pick-limit stamp
        cleared on draw, fetched through the same download path as
        primary picks. Escalation items (stamped tier_escalation)
        additionally respect the D-B2 per-tier download budget and
        the production ledger, and their assets record
        selection_via="tier_escalation" with their ladder tier.
        Returns the remaining shortfall.

        Phase 5 / AM-003: the VARIETY pass reuses this exact draw
        with ``_via="variety_pass"`` — the same eligibility
        re-checks, the same fetch path, the same budgets for
        escalation items; only the need arithmetic differs
        (``_need_fn`` recomputes the remaining DISTINCT-asset need
        instead of the coverage shortfall) and drawn candidates
        stamp variety_pick, so their records read
        selection_via="variety_pass". Accepted assets land in
        ``_sink`` when given, else the coverage list.

        Run-11 fixes 3+4: a budget gate that refuses an escalation
        item no longer skips it SILENTLY — the refusal is recorded
        in ``_budget_blocked`` (candidate, typed stop reason, tier)
        so the coverage record can tell a budget-stopped draw from
        a genuinely exhausted pool; the item keeps its
        pick_limit_reached stamp (it was never examined, and a
        later pass may still draw it). The ledger gate is
        can_draw_verified_download: this draw only ever fetches
        candidates from an already-verified pool, so the
        wall-clock guard — which gates NEW searches — does not
        strand the pool; the D-B2 production download cap and the
        fairness reserve still bind. Every fetch in the draw is
        counted in ``_draw_stats`` and the whole draw is bounded
        by the per-beat COVERAGE_DRAW_MAX_ATTEMPTS cap, recorded
        the same way when it binds."""
        _tier_attempts = {}
        _ledger = tier_ladder.ledger_for(registry)
        for _reserve in _items:
            if _shortfall <= 0:
                break
            if (
                _draw_stats is not None
                and _draw_stats.get("attempts", 0)
                >= COVERAGE_DRAW_MAX_ATTEMPTS
            ):
                if _budget_blocked is not None:
                    _budget_blocked.append({
                        "candidate_id": None,
                        "failure_class": "budget_exhausted",
                        "reason": STOP_BEAT_DOWNLOAD_ATTEMPT_CAP,
                        "tier": _reserve.get("tier_escalation"),
                        "undrawn": len(_items) - len(_drawn_ids),
                    })
                break
            _esc_tier = _reserve.get("tier_escalation")
            if _esc_tier:
                if (
                    _tier_attempts.get(_esc_tier, 0)
                    >= tier_ladder.TIER_DOWNLOAD_ATTEMPTS
                ):
                    if _budget_blocked is not None:
                        _budget_blocked.append({
                            "candidate_id": _reserve.get("candidate_id"),
                            "failure_class": "budget_exhausted",
                            "reason": (
                                tier_ladder.STOP_TIER_DOWNLOAD_BUDGET
                            ),
                            "tier": _esc_tier,
                        })
                    continue
                if not _ledger.can_draw_verified_download(
                    need_key=_escalation_need_key
                ):
                    if _budget_blocked is not None:
                        _budget_blocked.append({
                            "candidate_id": _reserve.get("candidate_id"),
                            "failure_class": "budget_exhausted",
                            "reason": (
                                tier_ladder
                                .STOP_PRODUCTION_DOWNLOAD_BUDGET
                            ),
                            "tier": _esc_tier,
                        })
                    continue
            _block = _uve_selection_block_reason(_reserve, registry)
            if _block is not None:
                # Truthful at draw time: the candidate WAS reached
                # and blocked by a live selection rule — that reason
                # replaces the pick-limit stamp on its record.
                _reserve["reject_reason"] = _block
                _reserve["rejection_stage"] = "selection"
                _blocked.append({
                    "candidate_id": _reserve.get("candidate_id"),
                    "reason": _block,
                })
                continue
            _reserve.pop("reject_reason", None)
            _reserve.pop("rejection_stage", None)
            if _via == "variety_pass":
                _reserve["variety_pick"] = True
            else:
                _reserve["coverage_pick"] = True
            if _esc_tier:
                _tier_attempts[_esc_tier] = (
                    _tier_attempts.get(_esc_tier, 0) + 1
                )
                _ledger.spend_download(need_key=_escalation_need_key)
            _fetch_item = dict(_reserve)
            if shot_need:
                _fetch_item["shot_need"] = dict(shot_need)
            _drawn_ids.append(_reserve.get("candidate_id"))
            if _draw_stats is not None:
                _draw_stats["attempts"] = (
                    _draw_stats.get("attempts", 0) + 1
                )
            _cov_asset = _uve_fetch_first(
                [_fetch_item], _uve_download_any_candidate, scene_dir,
                beat["sentence_numbers"][0],
                len(beat_videos) + len(beat_images) + 1,
                registry, used_keys, failures=_failures,
                transcript_scenario=scenario, transcript_bible=bible,
            )
            if _cov_asset:
                _cov_provider = str(
                    _cov_asset.get("provider")
                    or _cov_asset.get("source") or "unknown"
                )
                scene_stats.setdefault("providers", {}).setdefault(
                    _cov_provider,
                    {"discovered": 0, "verified": 0, "rejected": 0, "downloaded": 0, "download_failed": 0},
                )["downloaded"] += 1
                _cov_annotated = _uve_annotate(
                    _cov_asset, scenario, scenario_index,
                    beat.get("text", ""),
                )
                # B-roll B0: coverage-drawn assets carry their
                # tier exactly like primary picks (their
                # selection_via record, not their tier, is what
                # distinguishes how they were drawn). Escalation
                # assets carry their ladder tier and record the
                # route on the asset itself (B2). Phase 5: a variety
                # draw records its own route the same way — the
                # asset's tier still names the ladder tier it was
                # verified under; selection_via names the draw.
                if _esc_tier:
                    _cov_annotated["tier"] = str(_esc_tier)
                    _cov_annotated["selection_via"] = (
                        "variety_pass"
                        if _via == "variety_pass"
                        else "tier_escalation"
                    )
                else:
                    _cov_annotated["tier"] = str(
                        (shot_need or {}).get("tier") or "direct"
                    )
                    if _via == "variety_pass":
                        _cov_annotated["selection_via"] = "variety_pass"
                (
                    _sink
                    if _sink is not None
                    else coverage_accepted_assets
                ).append(_cov_annotated)
                # Phase 3 / AM-004: coverage/escalation accepts count
                # in the per-kind funnel's accepted stage too.
                _cov_kind = _cov_asset.get("_pool_kind") or (
                    "video"
                    if (_cov_asset.get("type") == "video"
                        or _cov_asset.get("visual_type") == "video")
                    else "image"
                )
                _kind_funnel.setdefault(
                    _cov_kind,
                    {"discovered": 0, "verified": 0, "rejected": 0,
                     "accepted": 0},
                )["accepted"] = _kind_funnel[_cov_kind].get("accepted", 0) + 1
                if _cov_asset.get("type") == "video" or _cov_asset.get("visual_type") == "video":
                    beat_videos.append(_cov_annotated)
                    print(f"      + video ({_via.replace('_', ' ')}, score {_cov_asset.get('universal_score')})")
                else:
                    beat_images.append(_cov_annotated)
                    print(f"      + image ({_via.replace('_', ' ')}, score {_cov_asset.get('universal_score')})")
                if _need_fn is not None:
                    _shortfall = _need_fn()
                else:
                    _shortfall = _uve_coverage_shortfall(
                        _sentence_count,
                        len(_uve_beat_distinct_asset_keys(
                            beat_videos, beat_images
                        )),
                    )
        return _shortfall

    if _shortfall > 0:
        _reserves = sorted(
            (
                item for item in combined_pool
                if item.get("reject_reason")
                == rejection_diagnostics.PICK_LIMIT_REASON
            ),
            key=lambda item: (
                item.get("selection_rank")
                if item.get("selection_rank") is not None
                else 1 << 30
            ),
        )
        _drawn_ids = []
        _blocked = []
        _budget_blocked = []
        _draw_stats = {"attempts": 0}
        _coverage_failures = []
        _shortfall = _draw_reserves(
            _reserves, _shortfall, _drawn_ids, _blocked,
            _coverage_failures,
            _budget_blocked=_budget_blocked, _draw_stats=_draw_stats,
        )
        _coverage_state = {
            "drawn_ids": _drawn_ids,
            "blocked": _blocked,
            "budget_blocked": _budget_blocked,
            "draw_stats": _draw_stats,
            "coverage_failures": _coverage_failures,
        }
    else:
        _coverage_state = None

    # Phase 2 measured saturation (see run_metrics): this pass's
    # evaluation evidence for the beat's sentences — evaluations
    # made, the verified candidates that were NEW to each sentence,
    # and the distinct query signatures executed. Measurement only:
    # no stopping rule reads it, and no budget derives from it.
    run_metrics.sentence_pass(
        list((beat or {}).get("sentence_numbers") or []),
        evaluations=len(_scored_all),
        verified_candidate_keys={
            str(_item.get("candidate_id"))
            for _item in _scored_all
            if (_item.get("verification") or {}).get("accepted")
            and _item.get("candidate_id")
        },
        query_signatures={
            _query_signature(_entry["query"]) for _entry in _query_log
        },
    )

    _state = {
        "scenario": scenario,
        "shot_need": shot_need,
        "search_cache": search_cache,
        "project_id": project_id,
        "bible": bible,
        "registry": registry,
        "used_keys": used_keys,
        "scene_dir": scene_dir,
        "scene_stats": scene_stats,
        "beat": beat,
        "beat_videos": beat_videos,
        "beat_images": beat_images,
        "scenario_index": scenario_index,
        "queries": queries,
        "review_records": review_records,
        "query_pass": query_pass,
        "raw_videos": raw_videos,
        "raw_web_sources": raw_web_sources,
        "raw_images": raw_images,
        "scored_videos": scored_videos,
        "scored_web_sources": scored_web_sources,
        "scored_images": scored_images,
        "combined_pool": combined_pool,
        "picks": picks,
        "fetch_failures": fetch_failures,
        "asset": asset,
        "accepted_asset": accepted_asset,
        "coverage_accepted_assets": coverage_accepted_assets,
        "variety_accepted_assets": variety_accepted_assets,
        "sentence_count": _sentence_count,
        "shortfall": _shortfall,
        "coverage_state": _coverage_state,
        "draw_reserves": _draw_reserves,
        "escalate_pool": _escalate_pool,
    }
    if phase == "initial":
        return accepted_asset, _state
    return _uve_search_and_pick_extended(_state)


def _uve_search_and_pick_extended(_state):
    """The EXTENDED phase of one discovery pass (Phase 2 split):
    tier-ladder escalation over the initial phase's pool, the
    variety draw, and the candidate-record / review accounting,
    finishing with the pass's return value (the primary asset, else
    the first coverage asset, else the first variety asset, else
    None — the pre-split contract).

    Consumes the state dict _uve_search_and_pick's initial phase
    assembled. The draw/escalation closures ride in the state: they
    capture the initial phase's frame, which stays alive through
    them, so escalation spends against the same ledger accounts and
    pool objects the initial phase built. Every historical caller
    reaches this through phase="full"; the scene driver calls it
    separately, only after every beat of the scene has had its
    initial phase (the fair-initial-pass guarantee)."""
    scenario = _state["scenario"]
    shot_need = _state["shot_need"]
    search_cache = _state["search_cache"]
    project_id = _state["project_id"]
    bible = _state["bible"]
    registry = _state["registry"]
    used_keys = _state["used_keys"]
    scene_dir = _state["scene_dir"]
    scene_stats = _state["scene_stats"]
    beat = _state["beat"]
    beat_videos = _state["beat_videos"]
    beat_images = _state["beat_images"]
    scenario_index = _state["scenario_index"]
    queries = _state["queries"]
    review_records = _state["review_records"]
    query_pass = _state["query_pass"]
    raw_videos = _state["raw_videos"]
    raw_web_sources = _state["raw_web_sources"]
    raw_images = _state["raw_images"]
    scored_videos = _state["scored_videos"]
    scored_web_sources = _state["scored_web_sources"]
    scored_images = _state["scored_images"]
    combined_pool = _state["combined_pool"]
    picks = _state["picks"]
    fetch_failures = _state["fetch_failures"]
    asset = _state["asset"]
    accepted_asset = _state["accepted_asset"]
    coverage_accepted_assets = _state["coverage_accepted_assets"]
    variety_accepted_assets = _state["variety_accepted_assets"]
    _sentence_count = _state["sentence_count"]
    _shortfall = _state["shortfall"]
    _coverage_state = _state["coverage_state"]
    _draw_reserves = _state["draw_reserves"]
    _escalate_pool = _state["escalate_pool"]

    if _coverage_state is not None:
        _drawn_ids = _coverage_state["drawn_ids"]
        _blocked = _coverage_state["blocked"]
        _budget_blocked = _coverage_state["budget_blocked"]
        _draw_stats = _coverage_state["draw_stats"]
        _coverage_failures = _coverage_state["coverage_failures"]

        # Tier ladder (B2): the draw above exhausted this pass's
        # own verified pool with sentences still uncovered — the
        # recorded shortfall is the ladder's only trigger. Derived
        # candidates join this pass's pool and scored lists, and
        # the same draw consumes them.
        _tier_attempts = []
        _escalation_accepted = 0
        if _shortfall > 0:
            _esc_items, _tier_attempts, _esc_scored = _escalate_pool(
                shot_need
            )
            if _esc_items:
                scored_videos.extend(_esc_scored["video"])
                scored_web_sources.extend(_esc_scored["web"])
                scored_images.extend(_esc_scored["image"])
                combined_pool.extend(_esc_items)
                _accepted_before = len(coverage_accepted_assets)
                _shortfall = _draw_reserves(
                    [
                        _item for _item in _esc_items
                        if (_item.get("verification") or {}).get("accepted")
                    ],
                    _shortfall, _drawn_ids, _blocked,
                    _coverage_failures,
                    _budget_blocked=_budget_blocked,
                    _draw_stats=_draw_stats,
                )
                _escalation_accepted = (
                    len(coverage_accepted_assets) - _accepted_before
                )
        if _coverage_failures:
            scene_stats.setdefault(
                "download_failures", []
            ).extend(_coverage_failures)
            for failure in _coverage_failures:
                entry = scene_stats.setdefault("providers", {}).setdefault(
                    str(failure.get("provider") or "unknown"),
                    {"discovered": 0, "verified": 0, "rejected": 0, "downloaded": 0, "download_failed": 0},
                )
                entry["download_failed"] += 1
            fetch_failures.extend(_coverage_failures)
        scene_stats.setdefault("coverage_passes", []).append({
            "beat_id": (beat or {}).get("beat_id"),
            "shot_index": scenario_index,
            "sentence_count": _sentence_count,
            "drawn": _drawn_ids,
            "blocked": _blocked,
            # Run-11 fix 3: budget-gate refusals during the draw
            # (per-tier attempt cap, production download cap,
            # per-beat attempt cap) — recorded with their typed
            # stop reasons, so a budget-stopped draw is never
            # mistaken for a genuinely exhausted pool; the draw's
            # own attempt count and cap ride along.
            "budget_blocked": _budget_blocked,
            "download_attempts": _draw_stats["attempts"],
            "download_attempt_cap": COVERAGE_DRAW_MAX_ATTEMPTS,
            "accepted": len(coverage_accepted_assets),
            "pool_exhausted": bool(_shortfall > 0),
            "covered_after": bool(_shortfall <= 0),
            # Master Directive Phase 2 / B2: the ladder's per-tier
            # outcomes for this pass (empty when the pass's own
            # pool covered — escalation never fired) and how many
            # of the accepted assets arrived via escalation.
            "tier_attempts": _tier_attempts,
            "escalation_accepted": _escalation_accepted,
        })

    # ------------------------------------------------------------
    # VARIETY PASS (Master Directive Phase 5 / AM-003 + FP-4):
    # coverage being POSSIBLE is not the same as a film worth
    # watching — a beat whose sentences can only be covered by
    # rotating a 1–2-card hand still repeats the same visuals for
    # its whole span (the repetition audit: founder films dealt
    # 4–7 distinct visuals per film, 53.7–59.6% repeated runtime).
    # When coverage is already secured, draw further DISTINCT
    # verifier-accepted assets from this pass's own pools — the
    # pick_limit_reached reserves and any verified ladder items
    # the coverage draw left behind — toward the FP-4 sufficiency
    # target of ⌈sentences/2⌉ distinct verified assets per beat.
    # Precedence is strict: primary picks first, coverage second,
    # variety last — relevance is never traded for novelty, the
    # draw is the coverage draw's own machinery (eligibility
    # re-checked per item, escalation budgets intact), and
    # rotation remains the timeline's fallback when the pool
    # holds no distinct verified asset. Every draw is stamped
    # selection_via="variety_pass" and recorded in
    # scene_stats["variety_passes"].
    # ------------------------------------------------------------
    if _shortfall <= 0 and _sentence_count >= 2:
        _variety_target = -(-_sentence_count // 2)
        _distinct_before = len(
            _uve_beat_distinct_asset_level_keys(beat_videos, beat_images)
        )
        _variety_need = _variety_target - _distinct_before
        if _variety_need > 0:
            _fetched_ids = {
                _a.get("candidate_id")
                for _a in list(beat_videos) + list(beat_images)
                if isinstance(_a, dict) and _a.get("candidate_id")
            }
            _variety_reserves = sorted(
                (
                    item for item in combined_pool
                    if (
                        item.get("reject_reason")
                        == rejection_diagnostics.PICK_LIMIT_REASON
                        or (
                            item.get("tier_escalation")
                            and (item.get("verification") or {}).get(
                                "accepted"
                            )
                            and not item.get("coverage_pick")
                            and not item.get("variety_pick")
                        )
                    )
                    and item.get("candidate_id") not in _fetched_ids
                ),
                key=lambda item: (
                    item.get("selection_rank")
                    if item.get("selection_rank") is not None
                    else 1 << 30
                ),
            )
            _variety_drawn = []
            _variety_blocked = []
            _variety_failures = []
            if _variety_reserves:

                def _variety_remaining():
                    return max(
                        0,
                        _variety_target
                        - len(_uve_beat_distinct_asset_level_keys(
                            beat_videos, beat_images
                        )),
                    )

                _variety_need = _draw_reserves(
                    _variety_reserves, _variety_need, _variety_drawn,
                    _variety_blocked, _variety_failures,
                    _via="variety_pass", _need_fn=_variety_remaining,
                    _sink=variety_accepted_assets,
                )
            if _variety_failures:
                scene_stats.setdefault(
                    "download_failures", []
                ).extend(_variety_failures)
                for failure in _variety_failures:
                    entry = scene_stats.setdefault(
                        "providers", {}
                    ).setdefault(
                        str(failure.get("provider") or "unknown"),
                        {"discovered": 0, "verified": 0,
                         "rejected": 0, "downloaded": 0,
                         "download_failed": 0},
                    )
                    entry["download_failed"] += 1
                fetch_failures.extend(_variety_failures)
            # Recorded whenever the target was unmet — including
            # an empty reserve pool (drawn == [] and pool_exhausted
            # say so), never silently skipped.
            scene_stats.setdefault("variety_passes", []).append({
                "beat_id": (beat or {}).get("beat_id"),
                "shot_index": scenario_index,
                "sentence_count": _sentence_count,
                "distinct_before": _distinct_before,
                "target_distinct": _variety_target,
                "drawn": _variety_drawn,
                "blocked": _variety_blocked,
                "accepted": len(variety_accepted_assets),
                "distinct_after": len(
                    _uve_beat_distinct_asset_level_keys(
                        beat_videos, beat_images
                    )
                ),
                "pool_exhausted": bool(_variety_need > 0),
            })
    if not asset and not coverage_accepted_assets and picks:
        # Shot-level download failure: neither the primary pick nor
        # the coverage pass produced an asset for this shot.
        scene_stats["downloads_failed"] += 1
    # VA-08: one traceable record per candidate outcome in this pass,
    # so the asset report's aggregates are built from the same records
    # an operator can inspect individually.
    _records = scene_stats.setdefault("candidate_records", [])
    # Stage 1 / R5: records are evaluation EVENTS (Architecture Review
    # C.A) — each carries its kind, the need's specificity/category,
    # the score (or gate_pre_scoring basis), the verifier's
    # contributions and contributing reasons, and the shot/sentence
    # linkage, so the asset report's rejection funnel can explain the
    # decision instead of merely counting it. The legacy keys
    # (candidate_id/url/provider/outcome/stage/reason) are unchanged.
    _need_category = story.resolve_visual_category(shot_need or scenario)
    _need_specificity = str(
        (shot_need or {}).get("specificity")
        or (scenario or {}).get("specificity")
        or ""
    )
    _sentence_numbers = list((beat or {}).get("sentence_numbers") or [])

    def _event_for(_item, _kind, _outcome, _stage, _reason):
        _verdict = _item.get("verification") or {}
        # Stage 6 / R5: contributing reasons from the collect-all
        # diagnostics — the gate's full failure list for gate events,
        # the verifier's failed_checks for scoring events; the
        # primary reason is excluded (it is the record's reason).
        # An empty list means "evaluated, nothing else failed";
        # None means the producing path carried no collect-all data.
        _contributing = None
        if _item.get("gate_failures"):
            _contributing = [
                f for f in _item["gate_failures"] if f != _reason
            ]
        elif _verdict.get("failed_checks") is not None:
            _contributing = [
                c for c in _verdict["failed_checks"] if c != _reason
            ]
        _event = rejection_diagnostics.build_event(
            candidate_id=_item.get("candidate_id"),
            url=(
                _item.get("url") or _item.get("video_url")
                or _item.get("image_url") or ""
            ),
            provider=str(
                _item.get("provider") or _item.get("source") or "unknown"
            ),
            kind=_kind,
            outcome=_outcome,
            stage=_stage,
            reason=_reason,
            score=_verdict.get("score"),
            contributions=_verdict.get("contributions"),
            verdict_reasons=_verdict.get("reasons"),
            contributing_reasons=_contributing,
            # Phase 1 / AM-007: the verdict's OWN per-dimension
            # record (the C.B.3 11-dimension shape) and its era
            # waiver pass through verbatim — build_event's
            # synthesised 3-key shape remains only for verdict-less
            # paths (gate events carry {"accepted", "score"} only,
            # so theirs still synthesises, truthfully unscored).
            dimension_results=_verdict.get("dimension_results"),
            era_waived=_verdict.get("era_waived"),
            # Phase 1 / AM-014: the candidate's title, so ledger
            # rows built from these records can carry it.
            title=str(_item.get("title") or ""),
            era_applicable=bool(
                (scenario or {}).get("era") or (shot_need or {}).get("era")
            ),
            transcript_check=_verdict.get("transcript_check"),
            specificity=_need_specificity,
            category=_need_category,
            matched_query=str(_item.get("matched_query") or ""),
            shot_index=scenario_index,
            beat_id=(beat or {}).get("beat_id"),
            sentence_numbers=_sentence_numbers,
            # Stage 7 / R8: the segment this event concerns, when
            # the item carries one (accepted/downloaded items do;
            # gate/scoring rejections predate selection -> None).
            segment_id=_item.get("segment_id"),
            # B-roll B0 (§6.1): the tier of the need this candidate
            # was evaluated for — "direct" for every search the
            # pipeline runs today. Phase 2 / B2: a candidate the
            # tier ladder produced carries its own tier stamp,
            # which wins over the parent need's.
            tier=str(
                _item.get("tier")
                or (shot_need or {}).get("tier")
                or "direct"
            ),
        )
        # Metadata-absence bypass (run-6345 fix): a speculative
        # candidate's events carry the bypass marker, the recorded
        # penalty and the absence reasons, so the asset report (and
        # the ledger built from it) can distinguish a conditional,
        # frame-confirmation-pending admission from an ordinary
        # metadata verification.
        if _item.get("metadata_absence_bypass"):
            _event["metadata_absence_bypass"] = True
            _event["metadata_absence_penalty"] = _item.get(
                "metadata_absence_penalty"
            )
            _event["absence_reasons"] = list(
                _item.get("absence_reasons") or []
            )
        return _event

    for _pool, _kind in (
        (scored_videos, "video"),
        (scored_web_sources, "web"),
        (scored_images, "image"),
    ):
        for _item in _pool:
            if _item.get("reject_reason"):
                _records.append(_event_for(
                    _item, _kind, "rejected",
                    _item.get("rejection_stage") or "scoring",
                    _item.get("reject_reason"),
                ))
            elif (_item.get("verification") or {}).get("accepted"):
                _records.append(_event_for(
                    _item, _kind, "verified", "verification", "",
                ))
    # Stage 6 / R5 + ASSET-016: selection, fetch and accepted
    # records are FULL events (score, contributions, specificity,
    # category, shot/sentence linkage), built through the same
    # _event_for as the verification-stage records — the minimal
    # hand-built dicts used here before could not explain a
    # selection decision. Pool items are indexed by candidate_id so
    # fetch failures (which carry only id/url/reason) rejoin the
    # candidate they belong to.
    _pool_by_candidate_id = {}
    for _item in combined_pool:
        if _item.get("candidate_id"):
            _pool_by_candidate_id.setdefault(_item["candidate_id"], _item)
    for _item in combined_pool:
        # Selection-stage rejections are stamped by _uve_pick on the
        # pool copies — including, since Stage 6, the pick-loop
        # remainder (pick_limit_reached); verification-stage ones
        # are already recorded from the scored lists above.
        if _item.get("rejection_stage") == "selection":
            _record = _event_for(
                _item,
                str(_item.get("_pool_kind") or ""),
                "rejected",
                "selection",
                _item.get("reject_reason") or "registry_block",
            )
            for _field in ("selection_rank", "reuse_penalty"):
                if _item.get(_field) is not None:
                    _record[_field] = _item.get(_field)
            if _item.get("_effective_score") is not None:
                _record["effective_score"] = _item.get("_effective_score")
            # Phase 3: the ranking signals that ordered this pool —
            # the bounded intent adjustment (+ its reasons) and the
            # AM-004 video preference — recorded on the event so a
            # selection decision is explainable from the report.
            if _item.get("_intent_adjustment"):
                _record["intent_adjustment"] = _item["_intent_adjustment"]
            if _item.get("_intent_reasons"):
                _record["intent_reasons"] = list(_item["_intent_reasons"])
            if _item.get("video_preference_applied"):
                _record["video_preference_applied"] = True
            _records.append(_record)
    for _failure in fetch_failures:
        _pool_item = _pool_by_candidate_id.get(_failure.get("candidate_id"))
        _stage = _failure.get("stage") or "download"
        if _pool_item is not None:
            _record = _event_for(
                _pool_item,
                str(_pool_item.get("_pool_kind") or ""),
                "download_failed",
                _stage,
                _failure.get("reason", ""),
            )
            if _pool_item.get("_intent_adjustment"):
                _record["intent_adjustment"] = _pool_item["_intent_adjustment"]
            if _pool_item.get("_intent_reasons"):
                _record["intent_reasons"] = list(_pool_item["_intent_reasons"])
            if _pool_item.get("video_preference_applied"):
                _record["video_preference_applied"] = True
            if _failure.get("segment_failure_cause"):
                _record["segment_failure_cause"] = _failure[
                    "segment_failure_cause"
                ]
            if _failure.get("technical_rejection"):
                _record["technical_rejection"] = _failure[
                    "technical_rejection"
                ]
            if _failure.get("frame_check"):
                # Phase 1.1: the frame-verification evidence behind
                # a frame-stage rejection rides on the record.
                _record["frame_check"] = _failure["frame_check"]
            _records.append(_record)
        else:
            _records.append({
                "candidate_id": _failure.get("candidate_id"),
                "url": _failure.get("url", ""),
                "provider": str(_failure.get("provider") or "unknown"),
                "outcome": "download_failed",
                "stage": _stage,
                "reason": _failure.get("reason", ""),
            })
    for _accepted in (
        [accepted_asset] if accepted_asset is not None else []
    ) + coverage_accepted_assets + variety_accepted_assets:
        _source_item = (
            _pool_by_candidate_id.get(_accepted.get("candidate_id"))
            or _accepted
        )
        _record = _event_for(
            _source_item,
            "video"
            if (
                _accepted.get("type") == "video"
                or _accepted.get("visual_type") == "video"
            )
            else "image",
            "accepted",
            "accepted",
            "",
        )
        if _accepted.get("segment_evidence"):
            _record["segment_evidence"] = _accepted["segment_evidence"]
        # Phase 1.1: an accepted asset carries its frame-check
        # record (the visual dimension, evaluated) into the ledger.
        _frame_check = _accepted.get("frame_check") or (
            _source_item.get("verification") or {}
        ).get("frame_check")
        if _frame_check:
            _record["frame_check"] = _frame_check
        # Metadata-absence bypass (run-6345 fix): an accepted
        # speculative asset records that frame verification
        # positively confirmed it — the condition its admission
        # was granted under.
        if _accepted.get("speculative_frame_confirmed"):
            _record["speculative_frame_confirmed"] = True
        # Phase 3: the accepted pick carries the ranking signals that
        # ordered it (intent adjustment + reasons, video preference).
        if _source_item.get("_intent_adjustment"):
            _record["intent_adjustment"] = _source_item["_intent_adjustment"]
        if _source_item.get("_intent_reasons"):
            _record["intent_reasons"] = list(_source_item["_intent_reasons"])
        if _source_item.get("video_preference_applied"):
            _record["video_preference_applied"] = True
        if _source_item.get("variety_pick"):
            # Phase 5 / AM-003: drawn by the variety pass from the
            # verified pool, after coverage was already secured —
            # the record names that route (and its rank), never the
            # pick_limit_reached rejection it carried before.
            _record["selection_via"] = "variety_pass"
            if _source_item.get("selection_rank") is not None:
                _record["selection_rank"] = _source_item["selection_rank"]
        elif _source_item.get("coverage_pick"):
            # Coverage fix: the record shows HOW this candidate came
            # to be used — drawn from the verified pool by the
            # coverage pass, at its recorded selection rank — never
            # left as the pick_limit_reached rejection it carried
            # before the draw. Phase 2 / B2: a candidate the tier
            # ladder produced records its own route.
            _record["selection_via"] = (
                "tier_escalation"
                if _source_item.get("tier_escalation")
                else "coverage_pass"
            )
            if _source_item.get("selection_rank") is not None:
                _record["selection_rank"] = _source_item["selection_rank"]
        _records.append(_record)

    review_records.append({
        "shot_index": scenario_index,
        "queries": list(queries or []),
        # F-4: the providers ACTUALLY present in this
        # scenario's pools (was hardcoded ["youtube", "web"]).
        "providers": sorted({
            str(c.get("provider") or c.get("source") or "unknown")
            for c in (raw_videos + raw_web_sources + raw_images)
        }) if shot_need else [],
        # VA-08: the rejected list is NOT capped — diagnostics must
        # cover every rejected candidate, each with its stable id and
        # the stage that rejected it.
        "rejected": [
            {
                "candidate_id": item.get("candidate_id"),
                "title": result_title(item),
                "reason": item.get("reject_reason"),
                "stage": item.get("rejection_stage") or "scoring",
            }
            for item in (scored_videos + scored_web_sources + scored_images)
            if item.get("reject_reason")
        ],
        "picked": [result_title(item) for item in beat_videos + beat_images][-2:],
    })

    _discovery_failures = drain_discovery_failures()
    if _discovery_failures:
        scene_stats.setdefault("discovery_failures", []).extend(_discovery_failures)
    # Stage 1 / R5: dedupe drops recorded during this pass's
    # verification become candidate_records events (stage "dedupe"),
    # so discovered-vs-evaluated arithmetic reconciles in the report.
    for _drop in drain_dedupe_drops():
        _records.append({
            **_drop,
            "primary_rejection_reason": _drop.get("reason", ""),
            "kind": "",
            "specificity": _need_specificity,
            "category": _need_category,
            "shot_index": scenario_index,
            "beat_id": (beat or {}).get("beat_id"),
            "sentence_numbers": _sentence_numbers,
        })
    # Stage 6 / C.A.3.4: the scene's rejection_reasons counter is a
    # DERIVED VIEW of the candidate records accumulated so far —
    # rebuilt from them, never incremented in parallel — so the
    # counter and the records cannot disagree.
    scene_stats["rejection_reasons"] = (
        rejection_diagnostics.primary_reason_counts(_records)
    )
    # Run-11 fix 2: the production's provider-health table so far
    # rides on the scene stats; the asset report publishes the
    # last (fullest) snapshot as the production's measured
    # per-provider download evidence.
    scene_stats["provider_health"] = provider_health.snapshot()
    if accepted_asset is not None:
        return accepted_asset
    if coverage_accepted_assets:
        # The shot produced its asset through the coverage pass —
        # callers deciding about further recovery must see the beat
        # as covered by this pass, exactly as a primary success.
        return coverage_accepted_assets[0]
    if variety_accepted_assets:
        # Defensive only: variety fires solely when coverage is
        # already secured, which implies a primary or coverage
        # asset exists — but if that ever changed, a shot whose
        # only assets arrived via variety must still read as
        # covered by this pass, never as a failure.
        return variety_accepted_assets[0]
    return None



MAX_COVERAGE_RECOVERY_ROUNDS = 2

_RECOVERY_STOPWORDS = {
    "about", "after", "before", "their", "there", "these", "those",
    "under", "until", "where", "which", "while", "would", "could",
    "should", "began", "every", "first", "other", "being", "between",
}


def _query_signature(query):
    """Word-set signature used to recognise already-tried queries."""
    return " ".join(
        sorted({w.lower() for w in str(query or "").split() if len(w) >= 3})
    )


# Quality Recovery Phase 2 (workstream D; DM-06): FACET signatures.
# A query's facet set is the set of meaningful search facets it uses —
# person / organisation / place / date / event / evidence-medium —
# derived from the beat's own shot contracts. Reformulation must
# change the facet set, not just reword: a pure rewording (same facet
# set, near-identical words) is recognised as already tried and never
# re-executed, and every executed recovery query is recorded with its
# facet signature alongside its string.
_FACET_MEDIUM_TERMS = {
    "photograph", "photo", "archive", "archival", "footage",
    "documentary", "newsreel", "image", "video",
}


def _facet_words(value):
    return {
        w.strip(".,;:!?\"'()").lower()
        for w in str(value or "").split()
        if len(w.strip(".,;:!?\"'()")) >= 3
    }


def _query_facets(query, beat=None):
    """The facet set a query uses, judged against the beat's contracts."""
    words = _facet_words(query)
    facets = set()
    if not words:
        return frozenset()
    if words & _FACET_MEDIUM_TERMS:
        facets.add("evidence")
    if any(re.fullmatch(r"(1[5-9]\d\d|20\d\d)s?", w) for w in words):
        facets.add("date")
    beat = beat or {}
    needs = beat.get("shot_needs") or []
    subject_hit = False
    for need in needs:
        subject_words = _facet_words(need.get("required_subject"))
        if subject_words and subject_words & words:
            subject_hit = True
            kind = str(need.get("required_type") or "").lower()
            facets.add(
                "organisation" if kind == "organization"
                else "person" if kind == "person"
                else "subject"
            )
        if _facet_words(need.get("place")) & words:
            facets.add("place")
        if _facet_words(need.get("event")) & words:
            facets.add("event")
        era_words = _facet_words(need.get("era"))
        if era_words & words:
            facets.add("date")
    if not subject_hit and _facet_words(
        str(beat.get("subject_id") or "").replace("_", " ")
    ) & words:
        facets.add("person")
    if _facet_words(beat.get("place")) & words:
        facets.add("place")
    return frozenset(facets)


def _facet_signature(query, beat=None):
    """Stable facet signature string for one query (sorted facets)."""
    return "|".join(sorted(_query_facets(query, beat)))


def _word_set(query):
    return {w for w in _query_signature(query).split() if w}


def _jaccard_words(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _recovery_queries(beat, tried_queries, broad=False, limit=4):
    """Alternative targeted queries for a beat that found no asset.

    Built deterministically from the beat's own shot contracts — the
    required subject anchored with place / era / event — never from
    generic filler. Round 1 keeps the era anchor; the broader round 2
    drops it and tries archive/photograph phrasings. Queries whose
    word-set signature was already tried are excluded, so recovery can
    never silently repeat the exact search that just failed.
    """
    needs = beat.get("shot_needs") or []
    beat_subject = str(beat.get("subject_id") or "").replace("_", " ").strip()
    beat_place = str(beat.get("place") or "").strip()
    candidates = []
    for need in needs:
        subject = str(need.get("required_subject") or "").strip()
        place = str(need.get("place") or beat_place).strip()
        era = str(need.get("era") or "").strip()
        event = str(need.get("event") or "").strip()
        if broad:
            templates = [
                (subject, place),
                (subject, "photograph"),
                (subject, place, "archive"),
            ]
        else:
            templates = [
                # Phase 2: the full-facet combination first — its
                # facet set {person, event, place, date} differs from
                # every initial-query facet set, so at least one
                # recovery query reformulates by FACETS, not wording
                # (required test 14(b)).
                (subject, event, place, era),
                (subject, place, era),
                (subject, event),
                (subject, place),
                (subject, era, "documentary"),
            ]
        for parts in templates:
            query = " ".join(p for p in parts if p).strip()
            if query:
                candidates.append(query)
    if beat_subject:
        if broad:
            candidates.append(f"{beat_subject} {beat_place} archive".strip())
        else:
            candidates.append(f"{beat_subject} {beat_place}".strip())
            candidates.append(f"{beat_subject} archive footage")
    if broad:
        text = str(beat.get("text") or "")
        words = [w.strip(".,;:!?\"'()") for w in text.split()]
        keywords = [
            w for w in words
            if len(w) >= 5 and w.lower() not in _RECOVERY_STOPWORDS
        ][:6]
        if keywords:
            candidates.append(" ".join(keywords))

    # Phase 2: a candidate is already-tried when its WORD signature
    # was tried, or when it is a pure rewording of a tried query —
    # same facet set with near-identical words (Jaccard >= 0.75, the
    # pipeline's existing near-duplicate threshold). A query whose
    # facet set is new is a genuine reformulation and executes.
    tried = [
        (_query_signature(q), _query_facets(q, beat), _word_set(q))
        for q in tried_queries
    ]
    tried_signatures = {sig for sig, _, _ in tried}
    out = []
    for query in candidates:
        signature = _query_signature(query)
        if not signature or signature in tried_signatures:
            continue
        facets = _query_facets(query, beat)
        words = _word_set(query)
        if any(
            facets == tried_facets
            and _jaccard_words(words, tried_words) >= 0.75
            for _, tried_facets, tried_words in tried
        ):
            continue
        tried_signatures.add(signature)
        tried.append((signature, facets, words))
        out.append(query)
        if len(out) >= limit:
            break
    return out


def _uve_unsearched_ladder_entry(
    beat, *, bible, registry, search_cache, project_id, used_keys,
    scene_dir, scene_stats, beat_videos, beat_images, review_records,
):
    """D-B3 entry (Intelligent B-Roll plan §6.1; Master Directive
    Phase 2): an unsearched-role beat ENTERS the tier ladder at T3.

    The planner's role semantics keep these sentences out of the
    direct search — that decision stands (never T1/T2). But a
    documentary still shows SOMETHING while they are spoken, so
    their merged fallback concepts (the beat's sentences' own
    concept sets, unioned) build ONE activity-tier derived need,
    run through the ordinary search/verify/fetch pass. The entry's
    queries and downloads are charged to the production escalation
    ledger (D-B2): queries before the pass, downloads by the
    pass's own deltas afterwards (accepted assets + recorded
    download failures). Returns the tier_attempts records for the
    beat (also stamped on its missing_visuals entries when the
    entry still leaves it uncovered)."""
    sentences = beat.get("sentences") or []
    merged = {
        "activities": [], "objects": [], "settings": [],
        "place_types": [], "era_terms": [], "mood": [],
    }
    intents = None
    for sentence in sentences:
        if not isinstance(sentence, dict):
            continue
        concepts = sentence.get("fallback_concepts")
        if not isinstance(concepts, dict):
            concepts = story.derive_fallback_concepts(
                get_sentence_text(sentence),
                place=beat.get("place"),
                entities=[],
            )
        for field in merged:
            for phrase in concepts.get(field) or []:
                phrase = str(phrase).strip()
                if phrase and phrase not in merged[field]:
                    merged[field].append(phrase)
        if intents is None and isinstance(
            sentence.get("visual_intents"), dict
        ):
            intents = sentence["visual_intents"]
    for field in merged:
        merged[field] = merged[field][:4]

    entry_tier = tier_ladder.entry_tier_for_role(beat.get("role"))
    ledger = tier_ladder.ledger_for(registry)
    attempt = {
        "tier": entry_tier,
        "queries": [],
        "accepted": 0,
        "outcome": None,
        "stop_reason": None,
        "entry": "unsearched_role",
    }
    probe_need = {
        "fallback_concepts": merged,
        "visual_intents": intents,
        "place": beat.get("place") or "",
        "era": "",
    }
    _pack_id, vocabulary = tier_ladder.broll_vocabulary_for(bible)
    derived = tier_ladder.derived_scenarios_for_tier(
        probe_need, bible, entry_tier, vocabulary=vocabulary,
    )
    if not derived:
        attempt["outcome"] = "no_concepts"
        return [attempt]
    raw = derived[0]
    first_sentence = (
        sentences[0]
        if sentences and isinstance(sentences[0], dict)
        else {}
    )
    plan = story.normalize_plan({
        "text": get_sentence_text(first_sentence),
        "role": beat.get("role") or "background",
        "subject_id": raw.get("subject_id"),
        "place": raw.get("place") or beat.get("place"),
        "time": raw.get("era"),
        "scenarios": [raw],
        "fallback_concepts": merged,
        "visual_intents": intents,
    }, get_sentence_text(first_sentence), bible, None)
    # The pipeline reads a beat's scenarios from its sentence
    # records (_beat_scenarios), so the derived scenario rides on a
    # synthetic copy of the beat's first sentence record, carrying
    # the merged concepts. Its role is set to a searchable one: the
    # ladder entry IS the deliberate T3 decision (D-B3), and the
    # role-based skip it bypasses is preserved on the need's
    # derived_from stamp and the beat's own records.
    synthetic_sentences = []
    for index, sentence in enumerate(sentences):
        record = dict(sentence) if isinstance(sentence, dict) else {}
        if index == 0:
            record["scenarios"] = plan["scenarios"]
            record["fallback_concepts"] = merged
            record["concepts_source"] = "ladder_entry"
            record["visual_intents"] = intents
            record["role"] = "background"
        synthetic_sentences.append(record)
    synthetic_beat = {
        "beat_id": beat.get("beat_id"),
        "searchable": True,
        "sentences": synthetic_sentences,
        "sentence_numbers": beat.get("sentence_numbers") or [],
        "subject_id": beat.get("subject_id"),
        "place": beat.get("place"),
        "role": beat.get("role"),
        "text": beat.get("text", ""),
    }
    needs = build_shot_needs(synthetic_beat, bible)
    if not needs:
        attempt["outcome"] = "no_concepts"
        return [attempt]
    need = needs[0]
    need["tier"] = entry_tier
    need["derived_from"] = {
        "entry": "unsearched_role",
        "role": beat.get("role"),
    }
    need["vocabulary_pack"] = _pack_id
    # The merged concepts ARE this need's concepts: stamping them
    # here is what lets the pass's own T4 escalation derive from
    # the beat's real concept set.
    need["fallback_concepts"] = merged
    need["concepts_source"] = "ladder_entry"
    need["visual_intents"] = intents
    scenario = (
        _beat_scenarios(synthetic_beat) or plan["scenarios"]
    )[0]
    # Breadth-first allocation: the entry's need key — the same
    # key this beat's own pick pass uses (scenario_index 1), so
    # the entry and any T4 continuation inside the pass share one
    # account, and the entry's first query is protected like every
    # other need's.
    _entry_key = (
        beat.get("beat_id"),
        need.get("shot_index") or 1,
        (beat.get("sentence_numbers") or [None])[0],
    )
    ledger.register_need(_entry_key)
    # D-B2: the entry tier's query budget, and the production
    # ledger's remaining search account, both bind the entry —
    # allocated breadth-first, so the entry's deepening cannot
    # drain the fairness reserve held for later scenes.
    budget = tier_ladder.TIER_QUERY_BUDGETS.get(entry_tier, 0)
    queries = list(need.get("queries") or [])[:budget]
    if not queries:
        attempt["outcome"] = "budget_exhausted"
        attempt["stop_reason"] = (
            tier_ladder.STOP_TIER_QUERY_BUDGET
            if budget <= 0
            else tier_ladder.STOP_PRODUCTION_SEARCH_BUDGET
        )
        return [attempt]
    # Attempt idempotency (run-10 fix): the identical entry batch
    # (same need scope — this beat — same tier, same normalised
    # query set) already completed in this production — its
    # results were verified and considered by the earlier
    # entry/walk. The entry does not re-spend; the
    # beat stays uncovered by the ladder and flows to
    # missing_visuals (and the T5 graphics route) exactly as an
    # exhausted entry does, with the repeat recorded.
    _prior_entry_attempt = ledger.completed_attempt(
        entry_tier, queries, need_key=_entry_key
    )
    if _prior_entry_attempt is not None:
        attempt["outcome"] = "pool_exhausted"
        attempt["repeat"] = True
        attempt["prior_outcome"] = _prior_entry_attempt.get("outcome")
        return [attempt]
    # Phase 3a / C-01 starvation fix: this gate used to call
    # can_spend_download() with NO need key, so the entry was
    # judged as an anonymous deepening spend against the whole
    # production account — earlier scenes' downloads could refuse
    # the entry before its first query ever ran, and the attempt
    # recorded an empty query list (the Petito sentence-23 shape).
    # The entry now gates as THIS need's breadth download (its
    # first download is protected like every other need's), and a
    # refusal records the planned queries beside the empty
    # executed list, so "planned but starved" is distinguishable
    # from "nothing to search" in every downstream record.
    if not ledger.can_spend_download(need_key=_entry_key):
        attempt["outcome"] = "budget_exhausted"
        attempt["stop_reason"] = (
            tier_ladder.STOP_WALL_CLOCK
            if ledger.wall_clock_exceeded()
            else tier_ladder.STOP_PRODUCTION_DOWNLOAD_BUDGET
        )
        attempt["queries_planned"] = list(queries)
        attempt["failure_class"] = "budget_exhausted"
        return [attempt]
    _entry_queries = list(queries)
    _allowed = ledger.allocate_searches(len(queries), need_key=_entry_key)
    queries = queries[:_allowed]
    if not queries:
        attempt["outcome"] = "budget_exhausted"
        attempt["stop_reason"] = tier_ladder.STOP_PRODUCTION_SEARCH_BUDGET
        attempt["queries_planned"] = list(_entry_queries)
        attempt["failure_class"] = "budget_exhausted"
        return [attempt]
    need["queries"] = queries
    scenario["queries"] = queries
    attempt["queries"] = list(queries)
    ledger.spend_searches(len(queries), need_key=_entry_key)
    failures_before = len(scene_stats.get("download_failures") or [])
    assets_before = len(beat_videos) + len(beat_images)
    ledger.begin_escalation()
    try:
        _uve_search_and_pick(
            scenario, need,
            search_cache=search_cache, project_id=project_id,
            bible=bible, registry=registry, used_keys=used_keys,
            scene_dir=scene_dir, scene_stats=scene_stats,
            beat=synthetic_beat,
            beat_videos=beat_videos, beat_images=beat_images,
            scenario_index=1, queries=queries,
            review_records=review_records,
            query_pass="unsearched_entry",
        )
    finally:
        ledger.end_escalation()
    gained = (len(beat_videos) + len(beat_images)) - assets_before
    failed = (
        len(scene_stats.get("download_failures") or []) - failures_before
    )
    for _ in range(gained + failed):
        ledger.spend_download(need_key=_entry_key)
    attempt["accepted"] = gained
    attempt["outcome"] = "covered" if gained else "pool_exhausted"
    # Attempt idempotency (run-10 fix): record the executed entry
    # batch as terminal, so a later walk (or a sibling beat's
    # entry) deriving the identical batch does not re-spend on it.
    # The entry fetches directly and builds no replayable pool —
    # the record's pool is empty by construction.
    ledger.record_attempt(
        entry_tier, _entry_queries,
        outcome="verified" if gained else "pool_exhausted",
        verified=gained,
        pool_items=[],
        need_key=_entry_key,
    )
    return [attempt]


def _raise_if_cancel_requested():
    """Cooperative cancellation inside the pipeline (Phase 8.4;
    design 03 §4.2): jobs.run_stage checks the ledger flag at stage
    boundaries, but the asset-manager stage is long — this check
    runs at scene entry and at each coverage-recovery round, so a
    cancelled production stops within one scene/round of work
    instead of grinding to the next stage. Raises
    run_ledger.RunCancelled, which the pipeline's existing handler
    routes to a cancelled finish. Fail-soft: any lookup problem
    means "not cancelled"."""
    try:
        from services import run_context as _run_context
        from services import run_ledger as _run_ledger
    except ImportError:
        return
    try:
        context = _run_context.current_context()
        run_id = getattr(context, "run_id", None) if context else None
        cancelled = bool(
            run_id and _run_ledger.is_cancel_requested(run_id)
        )
    except Exception:
        cancelled = False
    if cancelled:
        raise _run_ledger.RunCancelled(
            f"run {run_id} cancelled during asset retrieval"
        )


def _uve_process_scene_media(scene, project_dir, image_count=3, video_count=3):
    _raise_if_cancel_requested()
    if not isinstance(scene, dict):
        scene = {}
    scene_number = scene.get("scene_number", 1)
    scene_dir = os.path.join(project_dir, f"scene_{scene_number}")
    os.makedirs(scene_dir, exist_ok=True)

    # Phase 2: open this production's durable discovery memory (and
    # seed the run's failure guards from it) before any discovery
    # happens in this scene. Fail-soft: None means "no memory", and
    # the scene proceeds exactly as it did before Phase 2.
    _open_discovery_memory(project_dir)

    print()
    print("=" * 78)
    print(f"STORY-DRIVEN VISUAL ENGINE - SCENE {scene_number}")
    print("=" * 78)

    raw_sentences = get_sentence_list(scene)
    if not any(get_sentence_text(s) for s in raw_sentences):
        fallback = _uve_clean_text(scene.get("text") or scene.get("narration") or "")
        raw_sentences = [p.strip() for p in re.split(r"(?<=[.!?])\s+", fallback) if p.strip()]

    project_id = str(scene.get("project_id") or Path(project_dir).name)
    registry = get_registry(project_id)
    bible = _load_case_bible(scene, project_dir)
    beats = build_story_beats(raw_sentences, bible)
    write_beats_debug(beats, project_dir, scene_number=scene_number)
    # Breadth-first allocation (tier_ladder): register this scene's
    # needs with the production ledger BEFORE any beat spends, so
    # an early beat's deepening cannot consume the fairness slots
    # held for this scene's later beats. Registration spends
    # nothing; a beat that completes without escalating releases
    # its unspent keys at the end of its turn (below), so the
    # reserve never outlives the scene. Keys match the ones the
    # pick pass and the unsearched entry compute for the same
    # needs (beat id, shot index, first sentence number).
    _scene_ledger = tier_ladder.ledger_for(registry)

    def _beat_need_keys(_b):
        _first = ((_b or {}).get("sentence_numbers") or [None])[0]
        if (_b or {}).get("searchable", True):
            _keys = [
                (_b.get("beat_id"), _n.get("shot_index") or 1, _first)
                for _n in _b.get("shot_needs") or []
            ]
            return _keys or [(_b.get("beat_id"), 1, _first)]
        return [(_b.get("beat_id"), 1, _first)]

    for _b in beats:
        for _key in _beat_need_keys(_b):
            _scene_ledger.register_need(_key)
    print(f"BEATS: {len(beats)}")

    search_cache, used_keys = {}, set()
    # AUD-16/E: per-scene Asset Manager statistics. Every fallback and
    # failure is counted here and surfaces in the project asset report —
    # substitutions must never be silent.
    scene_stats = {
        "candidates_considered": 0,
        "candidates_rejected": 0,
        "downloads_failed": 0,
        "text_cards": 0,
    }
    all_images, all_videos, sentence_results = [], [], []
    results_by_number = {}

    # Phase 2 — the fair initial pass. The scene's beats run in TWO
    # passes over _beat_states: pass 1 (this loop) gives every beat
    # its INITIAL discovery — the unsearched-role ladder entry for
    # unsearchable beats, or the initial phase of every scenario's
    # search-and-pick for searchable ones — and pass 2 (the loop
    # after this one) runs the extended effort (each scenario's
    # extended phase, then coverage recovery) and the beat tails.
    # Extended effort for any sentence therefore cannot precede the
    # initial pass of another sentence in the scene (Phase 0 root
    # cause 3). Each beat also leaves pass 1 with a discovery_state
    # naming what actually happened to it, so a never-searched
    # sentence is distinguishable from a genuine coverage gap in
    # every downstream record.
    _beat_states = []
    for beat in beats:
        print()
        print("*" * 78)
        print(f"BEAT {beat['beat_id']}")
        print(f"  subject={beat.get('subject_id')} place={beat.get('place')} searchable={beat.get('searchable')}")
        print(f"  sentences={beat.get('sentence_numbers')}")
        for need in beat.get("shot_needs") or []:
            print(
                f"  NEED {need['shot_index']}: {need['shot_type']}/{need['specificity']} "
                f"subject={need.get('required_subject') or '-'} "
                f"place={need.get('place') or '-'} era={need.get('era') or '-'}"
            )
        print("*" * 78)

        scenarios = _beat_scenarios(beat)
        beat_videos, beat_images = [], []
        review_records = []
        _bstate = {
            "beat": beat,
            "scenarios": scenarios,
            "beat_videos": beat_videos,
            "beat_images": beat_images,
            "review_records": review_records,
            "pending_extended": [],
            "discovery_state": None,
        }
        _beat_states.append(_bstate)

        if not beat.get("searchable", True):
            _bstate["discovery_state"] = "unsearched_role"
            print(f"  NO SEARCH (role={beat.get('role')})")
            # B-roll B0 (§3.3/§11): an unsearched-role beat's
            # sentences are NAMED in the scene stats — they used to
            # vanish from every record upstream of the coverage
            # gate (the Gilgo sentence-1 shape). The naming stays.
            for _offset, _number in enumerate(
                beat.get("sentence_numbers") or []
            ):
                scene_stats.setdefault("uncovered_unsearched", []).append(
                    {
                        "sentence_number": _number,
                        "beat_id": beat.get("beat_id"),
                        "role": beat.get("role"),
                    }
                )
            # Master Directive Phase 2 / D-B3: the beat ENTERS the
            # tier ladder at T3 (activity tier — never T1/T2: the
            # role semantics that kept it unsearched stand). If the
            # entry still leaves it uncovered, its sentences join
            # missing_visuals with reason "unsearched_role" — the
            # SAME list every other uncovered sentence uses, so the
            # asset report's reconciliation stamps their
            # coverage_outcome and the T5 graphics route
            # (jobs._build_graphic_targets reads missing_visuals)
            # can reach them like any other missing entry.
            _entry_attempts = _uve_unsearched_ladder_entry(
                beat,
                bible=bible, registry=registry,
                search_cache=search_cache, project_id=project_id,
                used_keys=used_keys, scene_dir=scene_dir,
                scene_stats=scene_stats,
                beat_videos=beat_videos, beat_images=beat_images,
                review_records=review_records,
            )
            if not beat_videos and not beat_images:
                _numbers = beat.get("sentence_numbers") or []
                for _offset in range(len(beat.get("sentences") or [])):
                    scene_stats.setdefault("missing_visuals", []).append(
                        {
                            "sentence_number": (
                                _numbers[_offset]
                                if _offset < len(_numbers)
                                else None
                            ),
                            "reason": rejection_diagnostics.UNSEARCHED_ROLE,
                            "role": beat.get("role"),
                            "tier_attempts": _entry_attempts,
                            "discovery_state": "unsearched_role",
                        }
                    )
                    run_context.note_missing_visual()
        else:
            _searched_any = False
            for scenario_index, scenario in enumerate(scenarios, start=1):
                print(f"  SHOT {scenario_index}: [{scenario.get('specificity')}/{scenario.get('kind')}] {scenario.get('visual')}")
                shot_need = {}
                for need in beat.get("shot_needs") or []:
                    if need.get("shot_index") == scenario_index:
                        shot_need = need
                        break
                # Phase 2 (fair pass / gap honesty): the shot need
                # is resolved BEFORE the queries check, and the
                # effective query list is the need's queries, else
                # the scenario's, else the scenario's visual text —
                # the same fallback _uve_scenario_search itself
                # applies. The pre-Phase-2 order checked the
                # scenario's own queries first and silently skipped
                # the scenario when they were empty, even when its
                # shot need carried queries: the sentence was never
                # searched, yet surfaced downstream as an ordinary
                # coverage gap.
                queries = list(
                    shot_need.get("queries")
                    or scenario.get("queries")
                    or (
                        [scenario["visual"]]
                        if scenario.get("visual") else []
                    )
                )
                for query in queries:
                    print(f"      query: {query}")
                if not queries:
                    print("      (no queries - skipped)")
                    continue
                if shot_need.get("queries"):
                    scenario = dict(scenario)
                    scenario["queries"] = shot_need["queries"]
                print("      routed=youtube,web,images")
                _searched_any = True
                # Fair initial pass: only the INITIAL phase runs
                # here; the extended phase is deferred to pass 2,
                # after every beat of the scene has had its own
                # initial phase.
                _asset, _pending = _uve_search_and_pick(
                    scenario, shot_need,
                    search_cache=search_cache, project_id=project_id,
                    bible=bible, registry=registry, used_keys=used_keys,
                    scene_dir=scene_dir, scene_stats=scene_stats, beat=beat,
                    beat_videos=beat_videos, beat_images=beat_images,
                    scenario_index=scenario_index, queries=queries,
                    review_records=review_records,
                    phase="initial",
                )
                _bstate["pending_extended"].append(_pending)
            if not scenarios:
                _bstate["discovery_state"] = "no_scenarios"
            elif _searched_any:
                _bstate["discovery_state"] = "searched"
            else:
                _bstate["discovery_state"] = "no_queries"
            # The never-searched NAMING (uncovered_unsearched) and
            # the durable sentence states are written in pass 2,
            # from the beat's EFFECTIVE state: coverage recovery can
            # still search a beat the initial pass skipped, and only
            # a beat nothing ever searched may be named as such.

        # Phase 2: an unsearched-role beat's discovery state is
        # final here (its ladder entry above is its whole
        # discovery), so it is written to the durable memory's
        # sentence-state table immediately; searchable beats are
        # noted in pass 2 with their effective state.
        _store = _active_discovery_memory()
        if _store is not None and _bstate["discovery_state"] == "unsearched_role":
            for _number in beat.get("sentence_numbers") or []:
                try:
                    _store.note_sentence_state(
                        _number, _bstate["discovery_state"]
                    )
                except Exception:
                    pass

    # Pass 2 (Phase 2 fair initial pass): extended effort and beat
    # tails, in beat order, only now that every beat has had its
    # initial pass above.
    for _bstate in _beat_states:
        beat = _bstate["beat"]
        scenarios = _bstate["scenarios"]
        beat_videos = _bstate["beat_videos"]
        beat_images = _bstate["beat_images"]
        review_records = _bstate["review_records"]
        for _pending in _bstate["pending_extended"]:
            _uve_search_and_pick_extended(_pending)

        recovery_rounds_done = 0
        if beat.get("searchable", True) and not beat_videos and not beat_images and scenarios:
            # COVERAGE RECOVERY (founder directive): before a beat may
            # be declared uncovered, re-search it with alternative
            # targeted queries built from its own shot contracts.
            # Bounded rounds, never the same query twice, verification
            # and acceptance rules unchanged — recovery improves
            # DISCOVERY only. A beat is recorded missing only after
            # recovery is exhausted, and the attempts are reported.
            tried_queries = []
            for _sc in scenarios:
                tried_queries.extend(_sc.get("queries") or [])
            for _need in beat.get("shot_needs") or []:
                tried_queries.extend(_need.get("queries") or [])
            base_scenario = dict(scenarios[0])
            base_need = (beat.get("shot_needs") or [{}])[0]
            for round_no in range(1, MAX_COVERAGE_RECOVERY_ROUNDS + 1):
                _raise_if_cancel_requested()
                recovery_queries = _recovery_queries(
                    beat, tried_queries, broad=(round_no > 1),
                )
                if not recovery_queries:
                    # Phase 2 measured saturation: the recovery
                    # PLANNER has no untried query left for this
                    # beat — recorded as saturation evidence on the
                    # beat's sentences (measurement only; the
                    # round-count bound above is a scheduling
                    # limit, not evidence, and is not what fired).
                    run_metrics.sentence_recovery_exhausted(
                        beat.get("sentence_numbers") or []
                    )
                    break
                recovery_rounds_done = round_no
                # Stage 0 metrics: a recovery round is actually
                # being executed for this beat.
                run_metrics.coverage_recovery_round()
                tried_queries.extend(recovery_queries)
                print(
                    f"  COVERAGE RECOVERY round {round_no} for "
                    f"{beat['beat_id']}: {recovery_queries}"
                )
                recovery_scenario = dict(base_scenario)
                recovery_scenario["queries"] = recovery_queries
                recovered_asset = _uve_search_and_pick(
                    recovery_scenario, base_need,
                    search_cache=search_cache, project_id=project_id,
                    bible=bible, registry=registry, used_keys=used_keys,
                    scene_dir=scene_dir, scene_stats=scene_stats, beat=beat,
                    beat_videos=beat_videos, beat_images=beat_images,
                    scenario_index=int(base_need.get("shot_index") or 1),
                    queries=recovery_queries,
                    review_records=review_records,
                    query_pass="recovery",
                )
                scene_stats.setdefault("recovery_attempts", []).append(
                    {
                        "beat_id": beat.get("beat_id"),
                        "round": round_no,
                        "queries": recovery_queries,
                        # Phase 2: every executed query is recorded
                        # with its facet signature as well as its
                        # string, so reformulation is auditable and a
                        # tried signature is never re-executed.
                        "facet_signatures": [
                            _facet_signature(q, beat)
                            for q in recovery_queries
                        ],
                        "outcome": "covered" if recovered_asset else "no_asset",
                    }
                )
                if recovered_asset:
                    print(
                        f"  COVERAGE RECOVERED for {beat['beat_id']} "
                        f"in round {round_no}"
                    )
                    break

        # Phase 2: the beat's EFFECTIVE discovery state — the
        # initial-pass outcome, upgraded to "searched" when the beat
        # was covered or coverage recovery executed rounds for it
        # (recovery is real discovery: a beat recovery searched and
        # failed is a genuine gap, and its records must say
        # "searched"). The durable sentence states are noted with
        # this final state.
        _effective_discovery_state = _bstate["discovery_state"]
        if beat.get("searchable", True):
            if (
                _effective_discovery_state == "searched"
                or recovery_rounds_done > 0
                or beat_videos
                or beat_images
            ):
                _effective_discovery_state = "searched"
            _store = _active_discovery_memory()
            if _store is not None and _effective_discovery_state:
                for _number in beat.get("sentence_numbers") or []:
                    try:
                        _store.note_sentence_state(
                            _number, _effective_discovery_state
                        )
                    except Exception:
                        pass
        _bstate["effective_discovery_state"] = (
            _effective_discovery_state
        )

        if beat.get("searchable", True) and not beat_videos and not beat_images:
            # NO placeholder is created here. The generated text-card
            # fallback was removed permanently (founder directive):
            # a sentence with no verified visual is recorded
            # explicitly in scene_stats["missing_visuals"] and flows
            # into the asset report; the timeline covers the slot
            # with a genuine scene visual (rotation) or a renderer
            # holdover of real media — never a fabricated card.
            need = (beat.get("shot_needs") or [{}])[0]
            numbers = beat.get("sentence_numbers") or []
            # Phase 2 gap honesty: a beat that NOTHING ever searched
            # (the initial pass skipped it and recovery never ran
            # for it) NAMES its sentences in uncovered_unsearched
            # with the state as the reason — the same naming
            # unsearched-role beats get — so no downstream consumer
            # can mistake them for a genuine searched-and-failed
            # coverage gap.
            if _effective_discovery_state in (
                "no_queries", "no_scenarios"
            ):
                for _number in numbers:
                    scene_stats.setdefault(
                        "uncovered_unsearched", []
                    ).append(
                        {
                            "sentence_number": _number,
                            "beat_id": beat.get("beat_id"),
                            "role": beat.get("role"),
                            "reason": _effective_discovery_state,
                        }
                    )
            # Coverage fix: when this beat's verified pools were
            # drawn by the coverage pass and STILL came up empty,
            # the missing record says so — a coverage failure is
            # only honest when the pool's exhaustion is visible
            # beside it, not implied.
            _pool_exhausted = any(
                entry.get("pool_exhausted")
                for entry in scene_stats.get("coverage_passes") or []
                if entry.get("beat_id") == beat.get("beat_id")
            )
            # Phase 2 / B2: when this beat's passes escalated, the
            # ladder's per-tier outcomes ride on the missing record
            # too — a starved beat's report shows what was tried.
            _beat_attempts = []
            for _pass in scene_stats.get("coverage_passes") or []:
                if _pass.get("beat_id") == beat.get("beat_id"):
                    _beat_attempts.extend(
                        _pass.get("tier_attempts") or []
                    )
            # UV-05: a beat the ladder routed to graphics at the
            # atmospheric tier (graphic-preferred) is not a plain
            # coverage failure — its missing record says so, with
            # the plan signal that routed it, so the graphics
            # stage and the report can tell a deliberate routing
            # from an exhausted search.
            _graphic_preferred = None
            for _attempt in _beat_attempts:
                if _attempt.get("outcome") == tier_ladder.GRAPHICS_PREFERRED:
                    _graphic_preferred = _attempt.get(
                        "graphic_preferred"
                    ) or {}
                    break
            for offset in range(len(beat.get("sentences") or [])):
                _missing_entry = {
                    "sentence_number": (
                        numbers[offset] if offset < len(numbers) else None
                    ),
                    "reason": "no_verified_asset",
                    "specificity": str(need.get("specificity") or ""),
                    "recovery_rounds": recovery_rounds_done,
                    "coverage_pool_exhausted": bool(_pool_exhausted),
                    # Phase 2 gap honesty: the beat's EFFECTIVE
                    # discovery state (initial outcome upgraded by
                    # recovery/coverage — see the pass-2 block
                    # above), so the coverage assessment can
                    # distinguish a never-searched sentence from a
                    # genuine gap by data, not by which list the
                    # entry sits in.
                    "discovery_state": (
                        _effective_discovery_state or "searched"
                    ),
                }
                if _graphic_preferred is not None:
                    _missing_entry["reason"] = "graphic_preferred"
                    _missing_entry["graphic_preferred"] = dict(
                        _graphic_preferred
                    )
                if _beat_attempts:
                    _missing_entry["tier_attempts"] = list(_beat_attempts)
                scene_stats.setdefault("missing_visuals", []).append(
                    _missing_entry
                )
                run_context.note_missing_visual()
            print(
                "  MISSING VISUAL: no verified asset for this beat "
                "— recorded in the asset report, no placeholder created"
            )

        # Phase 3 / FP-4 (measure only): the beat's sufficiency —
        # distinct fetched assets against its sentence count, with
        # the FP-4 half-coverage reference point (a beat is
        # "sufficient" when at least half its sentences could be
        # served by a distinct asset). The coverage pass and the
        # tier ladder already did the drawing; this only records
        # the outcome, per beat, for the asset report.
        _sentence_total = len(beat.get("sentences") or [])
        _distinct_assets = len(
            _uve_beat_distinct_asset_keys(beat_videos, beat_images)
        )
        scene_stats.setdefault("beat_sufficiency", []).append({
            "beat_id": beat.get("beat_id"),
            "sentence_count": _sentence_total,
            "distinct_assets": _distinct_assets,
            "sufficiency_ratio": (
                round(_distinct_assets / _sentence_total, 4)
                if _sentence_total else None
            ),
            "meets_half": (
                _distinct_assets * 2 >= _sentence_total
                if _sentence_total else True
            ),
            "searchable": bool(beat.get("searchable", True)),
        })

        write_review_log(project_dir, scene_number, beat, review_records)

        # DM-17: deal each asset to the sentence that owns its
        # scenario (stamped scenario_index), never by raw position.
        _distribution = _distribute_beat_assets(
            beat, beat_videos, beat_images
        )

        for offset, sentence in enumerate(beat.get("sentences") or []):
            sentence_number = beat["sentence_numbers"][offset]
            people = _people_for_sentence(sentence, bible)

            # Assign each downloaded clip to the sentence/scenario that
            # produced it instead of copying the entire beat asset list
            # onto every sentence.
            sentence_videos, sentence_images = _distribution[offset]

            result = _uve_prepare_sentence_result(
                sentence,
                sentence_number,
                sentence_images,
                sentence_videos,
                people,
            )

            result["beat_id"] = beat["beat_id"]
            result["beat_searchable"] = beat.get("searchable", True)
            # B-roll B1 + Master Directive Phase 2 (§4.2): the
            # sentence's fallback concepts and visual-intention
            # record ride on its scene result, beside the tier
            # stamps — the report and the plan page read one shape.
            if isinstance(sentence, dict):
                if isinstance(sentence.get("fallback_concepts"), dict):
                    result["fallback_concepts"] = sentence[
                        "fallback_concepts"
                    ]
                    result["concepts_source"] = sentence.get(
                        "concepts_source"
                    )
                if isinstance(sentence.get("visual_intents"), dict):
                    result["visual_intents"] = sentence["visual_intents"]
            results_by_number[sentence_number] = result

        # Breadth-first allocation: this beat's turn is over — any
        # of its registered needs that never escalated release
        # their fairness slots (release_need only drops unspent
        # keys; a need that spent keeps its record).
        for _key in _beat_need_keys(beat):
            _scene_ledger.release_need(_key)

    for sentence_index, sentence in enumerate(raw_sentences, start=1):
        sentence_number = get_sentence_number(sentence, sentence_index)
        if not get_sentence_text(sentence):
            continue
        result = results_by_number.get(sentence_number)
        if not result:
            result = _uve_prepare_sentence_result(
                sentence, sentence_number, [], [], _people_for_sentence(sentence, bible)
            )
        sentence_results.append(result)
        all_images.extend(result.get("images") or [])
        all_videos.extend(result.get("videos") or [])

    all_visuals = []
    seen_paths = set()
    for asset in all_videos + all_images:
        path = str(asset.get("file_path") or "")
        if path and path in seen_paths:
            continue
        if path:
            seen_paths.add(path)
        item = dict(asset)
        item["visual_type"] = asset.get("visual_type") or asset.get("type") or "image"
        all_visuals.append(item)

    print()
    print("=" * 78)
    print(f"ENGINE COMPLETE - SCENE {scene_number}: beats={len(beats)} videos={len(all_videos)} images={len(all_images)} sentences={len(sentence_results)}")
    print("=" * 78)
    try:
        registry_summary = registry.summary()
    except Exception:
        registry_summary = {}

    result = {
        "scene_number": scene_number,
        "images": all_images, "videos": all_videos, "visuals": all_visuals,
        "sentences": sentence_results, "scene": scene,
        "beats": [
            {
                "beat_id": beat["beat_id"],
                "subject_id": beat.get("subject_id"),
                "place": beat.get("place"),
                # Coverage fix: the timeline's own-beat spanning
                # rule keys on the beat's role — carried here
                # additively so build_scene_timeline can read it
                # from the scene record it already receives.
                "role": beat.get("role"),
                "searchable": beat.get("searchable", True),
                "sentence_numbers": beat.get("sentence_numbers"),
                "shot_needs": beat.get("shot_needs") or [],
            }
            for beat in beats
        ],
        "asset_engine": "STORY_DRIVEN_VISUAL_V2",
        "asset_scope": "beat_verified",
        "sentence_count": len(sentence_results),
        "visual_engine_version": "STORY_DRIVEN_VISUAL_V2_BEATS",
        "registry_summary": registry_summary,
        "asset_stats": scene_stats,
        "case_bible": {
            "title": bible.get("title"),
            "story_type": bible.get("story_type"),
            "story_era": bible.get("story_era") or bible.get("time_period"),
            "tone": bible.get("tone"),
            "entity_ids": [e.get("id") for e in (bible.get("entities") or [])],
        },
    }
    return apply_review_decisions(result, project_dir, bible=bible)
def write_decision_template(project_dir, scene_number, beats):
    path = Path(project_dir) / f"scene_{scene_number}_decisions.json"
    if path.exists():
        return path
    rows = []
    for beat in beats or []:
        # MA-34: one row per SENTENCE — decisions are applied at
        # sentence granularity, so the template must let a reviewer
        # target one sentence without touching its beat siblings. A
        # beat with no sentence list keeps its single beat-level row.
        sentence_numbers = beat.get("sentence_numbers") or []
        if sentence_numbers:
            for number in sentence_numbers:
                rows.append({
                    "sentence_number": number,
                    "beat_id": beat.get("beat_id"),
                    "approve": True,
                    "swap_path": "",
                })
        else:
            rows.append({
                "beat_id": beat.get("beat_id"),
                "approve": True,
                "swap_path": "",
            })
    write_json_atomic(path, rows)
    print(f"DECISIONS TEMPLATE: {path}")
    return path


# Phase 1 / AM-013: the recorded default when a swap whose fresh
# verdict REJECTED is applied without the caller supplying an
# override reason. The swap is still applied (operator-supplied
# media workflows depend on it — blocking outright is NOT the
# policy), but the decision record must say, truthfully, that it
# was applied despite the rejection and why — or that no reason
# was recorded.
SWAP_OVERRIDE_DEFAULT_REASON = "applied_without_recorded_reason"


def _refresh_swapped_asset(asset, bucket, swap_path, decision, sentence_result, bible, override_reason=None):
    """MA-33: build the replacement record for a manual review swap.

    The replacement inherits NOTHING evidentiary from the original:

    - technical facts come from a FRESH probe of the new file
      (services.media_probe) — inherited duration/resolution/codec
      fields are cleared first, so nothing stale survives;
    - segment evidence is cleared (segment_evidence="none", no
      clip_start, no download provenance) — the replacement was
      never segment-verified;
    - rights are re-stamped honestly: licence "unknown", creator /
      source from the decision record when it supplies them, else
      "user_supplied", rights_status "verify_before_publish";
    - the verification verdict is REPLACED by a fresh
      story.verify_candidate run against the sentence's scenario
      (reached via the analyzed sentence embedded in the result).
      When no scenario is reachable, the verdict is an explicit
      rejection ("manual_swap_unverified") so every downstream
      filter treats the swap as unverified instead of inheriting
      the original's acceptance.

    Phase 1 / AM-013: when the fresh verdict REJECTS and the swap
    is nevertheless applied by the caller, the refreshed record
    carries a ``swap_override`` stamp — the rejection reason plus
    the override reason (the caller's ``override_reason``, or the
    decision record's own ``override_reason`` field, or the
    recorded default SWAP_OVERRIDE_DEFAULT_REASON). The stamp
    rides into the ledger's verification JSON and the certificate,
    so an overridden rejection is never silent. An ACCEPTED swap
    carries no stamp: nothing was overridden.

    Returns (refreshed_asset, verification_accepted), or
    (None, False) when the probe fails — the caller then leaves the
    original asset untouched and records the failed swap.
    """
    kind = bucket
    if bucket == "visuals":
        kind = (
            "videos"
            if (asset.get("visual_type") or asset.get("type")) == "video"
            else "images"
        )
    probe = probe_video(swap_path) if kind == "videos" else probe_image(swap_path)
    if not probe:
        return None, False

    refreshed = dict(asset)
    refreshed["file_path"] = swap_path
    refreshed["path"] = swap_path
    refreshed["swapped_from"] = {
        "file_path": asset.get("file_path") or asset.get("path") or "",
        "candidate_id": asset.get("candidate_id") or "",
    }
    refreshed["manual_swap"] = True

    for key in (
        "duration_seconds", "width", "height", "fps", "codec",
        "format", "has_audio",
    ):
        refreshed.pop(key, None)
    refreshed.update(probe)

    refreshed["segment_evidence"] = "none"
    for key in (
        "clip_start", "segment_evidence_basis",
        "source_duration_seconds", "download_method",
        "partial_download",
    ):
        refreshed.pop(key, None)

    refreshed["license"] = "unknown"
    refreshed["license_url"] = ""
    refreshed["creator"] = (
        decision.get("creator") or decision.get("source") or "user_supplied"
    )
    refreshed["source"] = decision.get("source") or "user_supplied"
    refreshed["source_url"] = decision.get("source_url") or ""
    refreshed["rights_status"] = "verify_before_publish"
    refreshed["retrieved_at"] = datetime.now(timezone.utc).isoformat()

    scenario = None
    embedded = sentence_result.get("sentence")
    if isinstance(embedded, dict):
        scenarios = _uve_scenarios_for(embedded)
        if scenarios:
            scenario = scenarios[0]
    candidate_meta = {
        "title": decision.get("title")
        or Path(swap_path).stem.replace("_", " ").replace("-", " "),
        "snippet": decision.get("snippet") or decision.get("description") or "",
        "description": decision.get("description") or decision.get("snippet") or "",
        "url": decision.get("source_url") or "",
        "source": refreshed["source"],
    }
    if scenario is not None:
        verification = story.verify_candidate(
            candidate_meta, scenario, bible or {}
        )
    else:
        verification = {
            "accepted": False,
            "reason": "manual_swap_unverified",
            "rejection_reason": "manual_swap_unverified",
        }
    refreshed["verification"] = verification
    refreshed["verification_status"] = (
        "verified" if verification.get("accepted") else "swapped"
    )
    if not verification.get("accepted"):
        # Phase 1 / AM-013: a rejected fresh verdict does not block
        # the swap (the caller applies it — user-supplied media
        # workflows depend on that), but the application is an
        # OVERRIDE and is recorded as one, with its reason.
        reason = (
            str(override_reason or "").strip()
            or str((decision or {}).get("override_reason") or "").strip()
            or SWAP_OVERRIDE_DEFAULT_REASON
        )
        refreshed["swap_override"] = {
            "applied_despite_rejection": True,
            "rejection_reason": (
                verification.get("rejection_reason")
                or verification.get("reason")
                or ""
            ),
            "override_reason": reason,
        }
    return refreshed, bool(verification.get("accepted"))


def apply_review_decisions(result, project_dir, bible=None):
    if not isinstance(result, dict):
        return result
    scene_number = result.get("scene_number", 1)
    path = Path(project_dir) / f"scene_{scene_number}_decisions.json"
    write_decision_template(project_dir, scene_number, result.get("beats") or [])
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return result

    # MA-34: decisions are keyed per SENTENCE first. The old code
    # indexed rows by beat_id alone, so one decision silently
    # overwrote EVERY sentence of a beat, and sentences whose
    # results carry no beat_id were never decided at all. A row
    # naming a sentence_number targets exactly that sentence; a
    # beat-only row remains the fallback for its sentences.
    by_sentence = {}
    by_beat = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("sentence_number") is not None:
            by_sentence[row.get("sentence_number")] = row
        elif row.get("beat_id") is not None:
            by_beat[row.get("beat_id")] = row

    outcomes = []
    aggregate_swaps = {}

    for sentence in result.get("sentences") or []:
        sentence_number = sentence.get("sentence_number")
        decision = None
        if sentence_number is not None and sentence_number in by_sentence:
            decision = by_sentence[sentence_number]
        elif sentence.get("beat_id") is not None:
            decision = by_beat.get(sentence.get("beat_id"))
        if not decision:
            continue
        if decision.get("approve", True) and not decision.get("swap_path"):
            continue
        swap = str(decision.get("swap_path") or "").strip()
        if swap and os.path.exists(swap):
            # MA-33: evidence-safe swap — see _refresh_swapped_asset.
            # A failed probe leaves the original asset untouched and
            # is recorded as a failed swap, never applied silently.
            for bucket in ("images", "videos", "visuals"):
                refreshed_bucket = []
                for asset in sentence.get(bucket) or []:
                    refreshed, accepted = _refresh_swapped_asset(
                        asset, bucket, swap, decision, sentence, bible
                    )
                    previous_path = (
                        asset.get("file_path") or asset.get("path") or ""
                    )
                    if refreshed is None:
                        outcomes.append({
                            "sentence_number": sentence_number,
                            "beat_id": sentence.get("beat_id"),
                            "swap_path": swap,
                            "previous_file_path": previous_path,
                            "previous_candidate_id": asset.get("candidate_id") or "",
                            "outcome": "swap_failed_probe",
                            "verification_accepted": False,
                        })
                        refreshed_bucket.append(asset)
                        continue
                    outcomes.append({
                        "sentence_number": sentence_number,
                        "beat_id": sentence.get("beat_id"),
                        "swap_path": swap,
                        "previous_file_path": previous_path,
                        "previous_candidate_id": asset.get("candidate_id") or "",
                        "outcome": "swapped",
                        "verification_accepted": accepted,
                        # Phase 1 / AM-013: when the fresh verdict
                        # rejected and the swap was applied anyway,
                        # the outcome record names the override and
                        # its recorded reason (None otherwise).
                        "swap_override": refreshed.get("swap_override"),
                    })
                    aggregate_swaps[previous_path] = refreshed
                    refreshed_bucket.append(refreshed)
                sentence[bucket] = refreshed_bucket
            continue
        sentence["images"] = []
        sentence["videos"] = []
        sentence["visuals"] = []
        sentence["rejected_by_review"] = True

    # MA-34: scene aggregates must reference the swapped files too —
    # otherwise the timeline's rotation pool resurrects the pre-swap
    # original straight from result["videos"]/["images"]/["visuals"].
    if aggregate_swaps:
        for bucket in ("videos", "images", "visuals"):
            entries = result.get(bucket)
            if not entries:
                continue
            synced = []
            for entry in entries:
                old_path = entry.get("file_path") or entry.get("path") or ""
                refreshed = aggregate_swaps.get(old_path)
                if refreshed is None:
                    synced.append(entry)
                    continue
                merged = dict(refreshed)
                if entry.get("visual_type"):
                    merged["visual_type"] = entry["visual_type"]
                synced.append(merged)
            result[bucket] = synced

    if outcomes:
        result["review_decision_outcomes"] = outcomes
    return result
def process_scene_media(scene, project_dir, image_count=3, video_count=3):
    """Canonical scene entry point (story-driven visual engine).

    Note (AUD-24): image_count/video_count are legacy parameters kept for
    call-site compatibility. They do NOT cap retrieval — the number of
    visuals is driven by the scene's story scenarios. They are documented
    here so nobody mistakes them for working controls.
    """
    return _uve_process_scene_media(scene, project_dir, image_count=image_count, video_count=video_count)












































