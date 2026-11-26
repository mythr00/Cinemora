"""Discovery seam of the former media_pipeline monolith (Phase 5.4).

Provider/search half of the pipeline: the per-provider search entry
points (SearXNG web/images/videos via services.search_provider,
Wikimedia Commons, Archive.org, YouTube), hit shaping and ranking,
title filters, the dedupe-drop record, the AM-021 per-query log stack,
and _uve_scenario_search — the scenario search that assembles a beat's
candidate pool from those providers.

Re-exported through media_pipeline; every historical
``media_pipeline.<name>`` reference (callers and the test suite's
monkeypatch contract) keeps working unchanged.

CALL-SITE CONVENTION — read before editing: the six provider entry
points (search_videos_multi, search_web_sources, search_image_sources,
search_wikimedia_images, search_wikimedia_videos,
search_archive_org_videos) and _record_discovery_failure are resolved
through media_pipeline's namespace at call time
(``_media_pipeline().<name>(...)``), NOT through this module's own
globals. The pipeline's established test contract patches those names
on media_pipeline and then drives _uve_scenario_search /
_uve_search_and_pick; resolving through media_pipeline preserves that
semantics bit-for-bit (unpatched, media_pipeline's attribute IS the
function defined here, re-exported). _record_discovery_failure stays
in media_pipeline with the per-run resilience state it owns (see the
Phase 5.4 note there: _DISCOVERY_FAILURES / _search_failure_state are
rebound by the single registered RunContext binder, which lives in
media_pipeline), so it is reached the same way.

_QUERY_LOG_STACK and _DEDUPE_DROPS live here: both are plain lists,
mutated in place only, never rebound — media_pipeline imports the
same objects, so the stack media_pipeline pushes onto is the stack
_uve_scenario_search reads.
"""

import os
import re

from services import rejection_diagnostics
from services import story_understanding as story


def _media_pipeline():
    """Lazy handle on media_pipeline (circular-import-safe; see the
    module docstring for why provider calls resolve through it)."""
    try:
        from services import media_pipeline
    except ImportError:  # flat-layout fallback
        import media_pipeline
    return media_pipeline


# Phase 3 / AM-021: the active per-query diagnostic logs (a stack —
# _uve_search_and_pick pushes its log for the duration of its search
# call and pops it after; _uve_scenario_search appends to the top).
# A context stack rather than a parameter: the search's signature
# (and every test double of it) stays untouched, and a stubbed
# search simply records nothing.
_QUERY_LOG_STACK: list = []

# Stage 1 / R5: candidates dropped by _uve_verify_all's dedupe
# (duplicate canonical key, or no key at all) previously vanished
# silently — they inflated "discovered" counts but appeared in no
# rejection record. They are now recorded with the same drain
# pattern and surfaced in scene_stats as dedupe_drops, so the
# funnel arithmetic (discovered vs evaluated) can be reconciled.
_DEDUPE_DROPS: list = []


def _record_dedupe_drop(candidate, key):
    _DEDUPE_DROPS.append(
        {
            "candidate_id": (candidate or {}).get("candidate_id"),
            "url": (
                (candidate or {}).get("url")
                or (candidate or {}).get("video_url")
                or (candidate or {}).get("image_url")
                or ""
            ),
            "provider": str(
                (candidate or {}).get("provider")
                or (candidate or {}).get("source")
                or "unknown"
            ),
            "outcome": "rejected",
            "stage": "dedupe",
            "reason": (
                rejection_diagnostics.DEDUPE_REASON
                if key
                else rejection_diagnostics.MISSING_KEY_REASON
            ),
        }
    )


def drain_dedupe_drops():
    """Return and clear the dedupe drops recorded since last drain."""
    drops = list(_DEDUPE_DROPS)
    _DEDUPE_DROPS.clear()
    return drops

FACE_WORDS = re.compile(r"\b(portrait|headshot|face|selfie|woman|man|girl|boy|model|actor|person|people|couple)\b", re.I)
ANIMAL_WORDS = re.compile(
    r"\b(chicken|chickens|rooster|hen|cow|cows|pig|pigs|goat|sheep|horse|barn|farm|farmer|livestock|duck|goose|puppy|kitten|cat|dog)\b", re.I)

def mentions_named_person(text):
    return bool(_media_pipeline()._analyze_current_text(text).get("entities"))


def looks_like_face(title):
    return bool(FACE_WORDS.search(str(title or "")))


def looks_like_animal(title):
    return bool(ANIMAL_WORDS.search(str(title or "")))


def blocked_visual(title, allow_people=False):
    title = str(title or "")
    if looks_like_animal(title):
        return True
    if allow_people or mentions_named_person(title):
        return False
    return looks_like_face(title)


def query_relevance(query, title):
    q = str(query or "").lower()
    t = str(title or "").lower()
    if not t:
        return 0.0
    tokens = [w for w in re.findall(r"[a-z0-9]+", q) if w not in _media_pipeline()._STOP and len(w) > 3]
    if not tokens:
        return 1.0
    return sum(1 for w in tokens if story._stem(w) in t or w in t) / len(tokens)

def _web_image_hit(url, title, source):
    # AUD-15: the licence of an arbitrary web image is UNKNOWN. This helper
    # previously stamped "royalty-free" on any URL — a false rights claim.
    return {"image_url": url, "url": url, "link": url, "title": title,
            "source": source, "license": "unknown", "snippet": title}


def _youtube_video_hit(url, title, source):
    # AUD-15: see _web_image_hit — never fabricate a licence.
    return {"video_url": url, "url": url, "link": url, "title": title,
            "source": source, "license": "unknown", "snippet": title}


def _rank_hits(query, hits, limit, allow_people=False):
    ranked = []
    for hit in hits:
        title = hit.get("title", "")
        if blocked_visual(title, allow_people=allow_people):
            continue
        score = query_relevance(query, title)
        if score < 0.20:
            continue
        ranked.append((score, hit))
    ranked.sort(key=lambda row: -row[0])
    return [hit for score, hit in ranked[:limit]]


def search_web_sources(query, limit=8):
    from services.search_provider import search_web

    query = str(query or "").strip()
    if not query:
        return []

    try:
        results = search_web(query)
    except Exception as error:
        print(f"Web source search failed: {error}")
        return []

    output = []

    for item in results[:limit]:
        url = str(item.get("url") or "").strip()
        if not url:
            continue

        provider, host = provider_for_url(url)
        output.append({
            "title": item.get("title", ""),
            "url": url,
            "link": url,
            "snippet": item.get("snippet", ""),
            "source": provider,
            "provider": provider,
            "source_domain": host,
            "license": "unknown",
            "rights_status": "verify_before_publish",
            "matched_query": query,
        })

    return output


def _web_source_is_video_candidate(candidate):
    """MS-2: can this web result plausibly yield moving footage?

    True for direct media files, known video hosts, and pages whose URL
    marks them as video pages (news /video/ and /av/ paths, /watch).
    Plain articles are NOT attempted — a failed or misleading extraction
    must never be how footage is 'found'."""
    url = str(
        candidate.get("url") or candidate.get("video_url") or candidate.get("link") or ""
    ).strip()
    if not url:
        return False
    if is_direct_media_url(url):
        return True
    provider, _host = provider_for_url(url)
    if provider in {"youtube", "vimeo", "dailymotion"}:
        return True
    from urllib.parse import urlparse

    path = (urlparse(url).path or "").lower()
    return "/video" in path or "/watch" in path or "/av/" in path


def search_image_sources(query, limit=5):
    """Real image candidates from SearXNG image search (AUD-13).

    Normalised to the same candidate shape as the other providers. The
    licence of a web image is NOT known, so it is recorded as "unknown" —
    never "royalty-free" (that false label existed in dead helper code and
    must not come back). Founder restriction: the permanently banned stock
    providers are never retrieved (ban documented in media_pipeline).
    """
    from services.search_provider import search_images

    query = str(query or "").strip()
    if not query:
        return []

    try:
        results = search_images(query, limit=max(limit * 2, 10))
    except Exception as error:
        print(f"Image search failed: {error}")
        return []

    output = []

    for item in results[:limit]:
        image_url = str(item.get("image_url") or "").strip()
        if not image_url:
            continue

        page_url = str(item.get("source_url") or item.get("link") or "").strip()

        output.append({
            "title": item.get("title", ""),
            "image_url": image_url,
            "url": image_url,
            "link": page_url,
            "source_url": page_url,
            "snippet": item.get("snippet", "") or item.get("title", ""),
            "source": item.get("source", ""),
            "type": "image",
            "license": "unknown",
            "rights_status": "verify_before_publish",
            "matched_query": query,
        })

    return output


def search_wikimedia_images(query, limit=5):
    """Wikimedia Commons candidates with REAL licence evidence (AUD-15).

    Commons is the fallback image source whose licence name, licence URL
    and creator come from the provider's own metadata — the only image
    source in the pipeline that can honestly record a licence. Everything
    else stays "unknown". No API key required; no banned providers.
    """
    from services.licensed_media import search_wikimedia

    query = str(query or "").strip()
    if not query:
        return []

    try:
        hits = search_wikimedia(query, limit=limit)
    except Exception as error:
        print(f"Wikimedia search failed: {error}")
        return []

    output = []
    for hit in hits:
        image_url = str(hit.get("download_url") or "").strip()
        if not image_url:
            continue

        creator = re.sub("<[^>]+>", "", str(hit.get("creator") or "")).strip()
        licence = str(hit.get("license") or "").strip()

        output.append({
            "title": hit.get("title", ""),
            "image_url": image_url,
            "url": image_url,
            "link": hit.get("source_url", ""),
            "source_url": hit.get("source_url", ""),
            "snippet": hit.get("alt", "") or hit.get("title", ""),
            "source": "wikimedia",
            "type": "image",
            "creator": creator,
            "license": licence or "unknown",
            "license_url": hit.get("license_url", ""),
            "rights_status": "license_recorded" if licence else "verify_before_publish",
            "matched_query": query,
        })

    return output


_KNOWN_VIDEO_PROVIDERS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "vimeo.com": "vimeo",
    "dailymotion.com": "dailymotion",
    "archive.org": "archive.org",
    "commons.wikimedia.org": "wikimedia",
    "upload.wikimedia.org": "wikimedia",
    "wikimedia.org": "wikimedia",
}


def provider_for_url(url):
    """(provider, host) for a URL. Known video hosts get canonical
    provider names; anything else is identified by its domain, so a news
    site is reported as itself (e.g. "bbc.com"), never as another
    provider. Multi-source correction MS-1."""
    from urllib.parse import urlparse

    host = (urlparse(str(url or "")).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for suffix, name in _KNOWN_VIDEO_PROVIDERS.items():
        if host == suffix or host.endswith("." + suffix):
            return name, host
    return (host or "web"), host


def _disabled_providers():
    """Providers disabled via CINEMORA_DISABLED_PROVIDERS (comma list).
    Provider toggles are part of the modular retrieval design: any
    provider — including YouTube — can be switched off without touching
    the others."""
    raw = os.getenv("CINEMORA_DISABLED_PROVIDERS", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def search_videos_multi(query, limit=5):
    """Multi-source video discovery (MS-1).

    SearXNG's video index covers many hosts — YouTube, Vimeo, news sites,
    archives and more. The previous implementation DISCARDED every result
    that was not a YouTube watch URL and stamped the survivors
    source="youtube". This function preserves every result, tags it with
    its real provider/domain, and lets the shared gate/verifier/pick
    machinery judge all of them by relevance alone. YouTube has no
    special status here.
    """
    query = str(query or "").strip()
    if not query:
        return []

    from services.search_provider import search_videos

    disabled = _disabled_providers()
    results = []
    seen = set()

    try:
        hits = search_videos(query, limit=max(limit * 3, 15))
    except Exception as error:
        print(f"Video search failed: {error}")
        return []

    for hit in hits:
        url = str(
            hit.get("video_url")
            or hit.get("url")
            or hit.get("link")
            or ""
        ).strip()

        if not url:
            continue

        provider, host = provider_for_url(url)
        if provider in disabled:
            continue

        if provider == "youtube" and "&" in url:
            url = url.split("&", 1)[0]

        dedupe_key = url.split("#", 1)[0]
        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)

        results.append({
            "video_url": url,
            "url": url,
            "link": url,
            "title": hit.get("title", ""),
            "source": provider,
            "provider": provider,
            "source_domain": host,
            # SearXNG's per-result "source" is the channel/site name —
            # keep it as creator evidence (AUD-15).
            "channel": hit.get("source", ""),
            "creator": hit.get("source", ""),
            "license": "unknown",
            "rights_status": "verify_before_publish",
            "snippet": hit.get("snippet", ""),
            "date": hit.get("date", ""),
            "duration": hit.get("duration", ""),
            "matched_query": query,
        })

        if len(results) >= limit:
            break

    return results


def search_wikimedia_videos(query, limit=5):
    """Wikimedia Commons VIDEO candidates (MS-4) with provider licence
    evidence. Supplementary provider: Commons is one source among many,
    with no priority over or under any other."""
    from services.licensed_media import search_wikimedia_videos as _search

    query = str(query or "").strip()
    if not query:
        return []

    output = []
    for hit in _search(query, limit=limit):
        video_url = str(hit.get("download_url") or "").strip()
        if not video_url:
            continue
        creator = re.sub("<[^>]+>", "", str(hit.get("creator") or "")).strip()
        licence = str(hit.get("license") or "").strip()
        output.append({
            "title": hit.get("title", ""),
            "video_url": video_url,
            "url": hit.get("source_url") or video_url,
            "source_url": hit.get("source_url") or "",
            "source": "wikimedia",
            "provider": "wikimedia",
            "creator": creator,
            "channel": creator,
            "license": licence if licence else "unknown",
            "license_url": hit.get("license_url") or "",
            "rights_status": "recorded" if licence else "verify_before_publish",
            "snippet": hit.get("alt") or "",
            "matched_query": query,
        })
    return output


def search_archive_org_videos(query, limit=5):
    """Archive.org moving-image candidates (MS-4) — an independent
    provider with its own search + metadata, licence evidence included
    when the item records it. Default limit matches the Wikimedia
    video supplementary limit (Gate 2 parity, 2026-11-26)."""
    from services.archive_org import search_archive_videos

    query = str(query or "").strip()
    if not query:
        return []
    return search_archive_videos(query, limit=limit)


def search_pexels_videos(query, limit=5):
    """Pexels stock VIDEO candidates (Gate 2 WS4, founder decision
    2026-11-26, Annex B: the IA-05 ban is lifted). Keyless no-op:
    returns [] until PEXELS_API_KEY is configured. Illustrative
    stock only — candidates are stamped as such by the provider
    module and get no verification bypass."""
    from services.stock_providers import search_pexels_videos as _search

    query = str(query or "").strip()
    if not query or "pexels" in _disabled_providers():
        return []
    return _search(query, limit=limit)


def search_pexels_images(query, limit=5):
    """Pexels stock PHOTOGRAPH candidates (see search_pexels_videos)."""
    from services.stock_providers import search_pexels_images as _search

    query = str(query or "").strip()
    if not query or "pexels" in _disabled_providers():
        return []
    return _search(query, limit=limit)


def search_pixabay_videos(query, limit=5):
    """Pixabay stock VIDEO candidates (Gate 2 WS4, founder decision
    2026-11-26, Annex B). Keyless no-op until PIXABAY_API_KEY is
    configured; illustrative stock only (see search_pexels_videos)."""
    from services.stock_providers import search_pixabay_videos as _search

    query = str(query or "").strip()
    if not query or "pixabay" in _disabled_providers():
        return []
    return _search(query, limit=limit)


def search_pixabay_images(query, limit=5):
    """Pixabay stock PHOTOGRAPH candidates (see search_pixabay_videos)."""
    from services.stock_providers import search_pixabay_images as _search

    query = str(query or "").strip()
    if not query or "pixabay" in _disabled_providers():
        return []
    return _search(query, limit=limit)


def _resolve_stock_provider(name, fallback):
    """Resolve a stock entry point through media_pipeline's namespace
    when it is re-exported there (the established call-site
    convention), falling back to this module's own wrapper. The
    fallback keeps stock discovery live without a media_pipeline
    edit; a test double patched onto media_pipeline still wins."""
    fn = getattr(_media_pipeline(), name, None)
    return fn if callable(fn) else fallback


def search_youtube_videos(query, limit=5):
    """Legacy name kept for existing callers. Since the multi-source
    correction (MS-1) this returns results from ALL video providers via
    search_videos_multi — it is no longer YouTube-only despite the name."""
    return _media_pipeline().search_videos_multi(query, limit=limit)


def search_source_videos(query, limit=5):
    return search_youtube_videos(query, limit=limit)


def _slug_title(page_url, fallback):
    match = re.search(r"/video/(.+?)-\d+/?$", str(page_url or ""))
    return match.group(1).replace("-", " ") if match else fallback





def is_direct_media_url(url):
    low = str(url or "").lower().split("?", 1)[0]
    return low.endswith((".mp4", ".mov", ".webm", ".m4v", ".jpg", ".jpeg", ".png", ".webp"))

def _uve_scenario_search(
    scenario,
    search_cache,
    project_id,
    shot_need=None,
):
    """
    Simple documentary search.

    Sources:
      - YouTube
      - general web sources

    Search queries come directly from the visual
    scenario / shot contract. No legacy provider
    routing or universal-search layer is used.
    """

    need = shot_need or {}

    queries = list(
        need.get("queries")
        or scenario.get("queries")
        or []
    )

    # Phase 3 / AM-021: per-query diagnostics. While a query log is
    # active (pushed by _uve_search_and_pick — a context stack, not
    # a parameter, so this function's signature and every test
    # double of it stay untouched), every query ACTUALLY issued is
    # appended as {"query", "source", "hits"} in issue order —
    # source from the need's query_sources (stamped by
    # build_shot_needs: scenario / composed / intent_queries;
    # search-internal queries carry their own labels), hits = raw
    # results returned for it, before cross-query dedupe.
    # Supplementary-provider hits fold into the entry of the query
    # they were issued for (Gate 2: supplementary providers now run
    # on every unique query, not only the primary).
    query_log = _QUERY_LOG_STACK[-1] if _QUERY_LOG_STACK else None
    _query_sources = {}
    for _qs in (need.get("query_sources") or []):
        if isinstance(_qs, dict) and _qs.get("query"):
            _query_sources[" ".join(str(_qs["query"]).split())] = str(
                _qs.get("source") or "scenario"
            )
    _log_by_query = {}

    def _log_query(_q, _source, _hits):
        if query_log is None:
            return
        _entry = _log_by_query.get(_q)
        if _entry is None:
            _entry = {"query": _q, "source": _source, "hits": 0}
            _log_by_query[_q] = _entry
            query_log.append(_entry)
        _entry["hits"] += int(_hits or 0)

    # If a scenario somehow has no generated query,
    # use its visual text as the final generic fallback.
    if not queries:
        visual = str(
            scenario.get("visual")
            or ""
        ).strip()

        if visual:
            queries = [visual]

    videos = []
    web_sources = []
    images = []

    seen_video_urls = set()
    seen_web_urls = set()
    seen_image_urls = set()

    # Remove duplicate / near-duplicate search queries before
    # spending SearXNG calls. Keep the first useful wording.
    unique_queries = []
    query_signatures = []

    for raw_query in queries:
        query = " ".join(
            str(raw_query or "").split()
        ).strip()

        if not query:
            continue

        words = sorted(
            {
                word.lower()
                for word in query.split()
                if len(word) >= 3
            }
        )

        signature = " ".join(words)

        if not signature:
            continue

        duplicate = False

        for existing in query_signatures:
            existing_words = set(existing.split())
            current_words = set(signature.split())

            if not existing_words or not current_words:
                continue

            overlap = (
                len(existing_words & current_words)
                / max(
                    len(existing_words | current_words),
                    1,
                )
            )

            if overlap >= 0.75:
                duplicate = True
                break

        if duplicate:
            continue

        query_signatures.append(signature)
        unique_queries.append(query)

        if len(unique_queries) >= 4:
            break

    for query in unique_queries:
        query = " ".join(
            str(query or "").split()
        ).strip()

        if not query:
            continue

        cache_key = (
            f"simple_sources::{query.lower()}"
        )

        if cache_key in search_cache:
            cached = search_cache[
                cache_key
            ]

            _log_query(
                query,
                _query_sources.get(query, "scenario"),
                len(cached.get("videos") or [])
                + len(cached.get("web_sources") or [])
                + len(cached.get("images") or []),
            )

            videos.extend(
                cached.get("videos") or []
            )

            web_sources.extend(
                cached.get("web_sources") or []
            )

            images.extend(
                cached.get("images") or []
            )

            continue

        try:
            found_videos = _media_pipeline().search_videos_multi(
                query,
                limit=8,
            )
        except Exception as error:
            _media_pipeline()._record_discovery_failure("searxng-videos", query, error)
            print(
                f"Video search failed "
                f"for '{query}': {error}"
            )
            found_videos = []

        try:
            found_web = _media_pipeline().search_web_sources(
                query,
                limit=5,
            )
        except Exception as error:
            _media_pipeline()._record_discovery_failure("searxng-web", query, error)
            print(
                f"Web search failed "
                f"for '{query}': {error}"
            )
            found_web = []

        try:
            found_images = _media_pipeline().search_image_sources(
                query,
                limit=5,
            )
        except Exception as error:
            _media_pipeline()._record_discovery_failure("searxng-images", query, error)
            print(
                f"Image search failed "
                f"for '{query}': {error}"
            )
            found_images = []

        # Wikimedia Commons supplement (AUD-15): appended after SearXNG
        # results; carries genuine licence evidence when available.
        try:
            found_images = found_images + _media_pipeline().search_wikimedia_images(
                query,
                limit=3,
            )
        except Exception as error:
            _media_pipeline()._record_discovery_failure("wikimedia", query, error)
            print(
                f"Wikimedia search failed "
                f"for '{query}': {error}"
            )

        search_cache[cache_key] = {
            "videos": found_videos,
            "web_sources": found_web,
            "images": found_images,
        }

        _log_query(
            query,
            _query_sources.get(query, "scenario"),
            len(found_videos) + len(found_web) + len(found_images),
        )

        for video in found_videos:
            url = str(
                video.get("video_url")
                or video.get("url")
                or video.get("link")
                or ""
            ).strip()

            if not url:
                continue

            if url in seen_video_urls:
                continue

            seen_video_urls.add(url)
            videos.append(video)

        for source in found_web:
            url = str(
                source.get("url")
                or source.get("link")
                or ""
            ).strip()

            if not url:
                continue

            if url in seen_web_urls:
                continue

            seen_web_urls.add(url)
            web_sources.append(source)

        for image in found_images:
            image_url = str(
                image.get("image_url")
                or ""
            ).strip()

            if not image_url:
                continue

            if image_url in seen_image_urls:
                continue

            seen_image_urls.add(image_url)
            images.append(image)

    # F-3: format-aware discovery. When the scenario's format intent is
    # a map or a document with real confidence, run ONE additional
    # intent-shaped image search so those formats are sought
    # deliberately instead of found by luck. The shaped query is a
    # discovery aid only — classification of anything it returns still
    # requires metadata/source evidence (classify_visual_format).
    _intent = str(scenario.get("format_intent") or "any").lower()
    try:
        _intent_conf = float(scenario.get("format_intent_confidence") or 0)
    except (TypeError, ValueError):
        _intent_conf = 0.0
    if _intent in {"map", "document"} and _intent_conf >= 0.5 and unique_queries:
        shaped_query = f"{unique_queries[0]} {_intent}"
        shaped_images = []
        try:
            shaped_images = _media_pipeline().search_image_sources(shaped_query, limit=5)
        except Exception as error:
            _media_pipeline()._record_discovery_failure("searxng-images", shaped_query, error)
            print(f"Shaped image search failed for '{shaped_query}': {error}")
        try:
            shaped_images = shaped_images + _media_pipeline().search_wikimedia_images(
                shaped_query, limit=3
            )
        except Exception as error:
            _media_pipeline()._record_discovery_failure("wikimedia", shaped_query, error)
            print(f"Shaped Wikimedia search failed for '{shaped_query}': {error}")
        _log_query(shaped_query, "format_shaped", len(shaped_images))
        for image in shaped_images:
            image_url = str(image.get("image_url") or "").strip()
            if not image_url or image_url in seen_image_urls:
                continue
            seen_image_urls.add(image_url)
            images.append(image)

    # MS-4 / Gate 2 (footage-discovery directive, 2026-11-26):
    # supplementary independent providers, queried for EVERY unique
    # scenario query — the same queries the SearXNG channels run on —
    # not only the primary query. YouTube is one source among many;
    # the keyless archival providers (Archive.org, Wikimedia Commons
    # video) get parity of opportunity per query. Each provider call
    # is failure-isolated per query: one provider being down or
    # empty never blocks the others or the scenario, and none of
    # them is the centre of discovery — they join the same pool.
    # Budget accounting: there is no separate supplementary budget
    # guard in this function; these searches are counted exactly as
    # they were when they ran on the primary query only — through
    # the providers' own run_metrics hooks (archive_org_search /
    # wikimedia_search inside the provider modules) and through the
    # AM-021 per-query log below, folded into the entry of the query
    # they were issued for.
    if unique_queries:
        primary_query = unique_queries[0]

        def _merge_videos(found):
            for video in found or []:
                url = str(
                    video.get("video_url") or video.get("url") or ""
                ).strip()
                if not url or url in seen_video_urls:
                    continue
                seen_video_urls.add(url)
                videos.append(video)

        for supplementary_query in unique_queries:
            try:
                _archive_hits = _media_pipeline().search_archive_org_videos(
                    supplementary_query, limit=5
                )
                _log_query(
                    supplementary_query,
                    _query_sources.get(supplementary_query, "scenario"),
                    len(_archive_hits or []),
                )
                _merge_videos(_archive_hits)
            except Exception as error:
                _media_pipeline()._record_discovery_failure(
                    "archive.org", supplementary_query, error
                )
                print(
                    f"Archive.org search failed "
                    f"for '{supplementary_query}': {error}"
                )

            try:
                _wikimedia_hits = _media_pipeline().search_wikimedia_videos(
                    supplementary_query, limit=5
                )
                _log_query(
                    supplementary_query,
                    _query_sources.get(supplementary_query, "scenario"),
                    len(_wikimedia_hits or []),
                )
                _merge_videos(_wikimedia_hits)
            except Exception as error:
                _media_pipeline()._record_discovery_failure(
                    "wikimedia", supplementary_query, error
                )
                print(
                    f"Wikimedia video search failed "
                    f"for '{supplementary_query}': {error}"
                )

            # Gate 2 WS4 (Annex B): stock providers — Pexels and
            # Pixabay, videos AND images — queried per unique query
            # with the same failure isolation, counting and pool
            # treatment as the other supplementary channels. They
            # are keyless no-ops until their API keys are configured
            # and honour CINEMORA_DISABLED_PROVIDERS via the
            # wrappers above. Stock hits carry the illustrative-
            # stock stamp and pass through the same verification
            # gates as every other candidate.
            for _stock_name, _stock_provider in (
                ("search_pexels_videos", "pexels"),
                ("search_pixabay_videos", "pixabay"),
            ):
                try:
                    _stock_hits = _resolve_stock_provider(
                        _stock_name, globals()[_stock_name]
                    )(supplementary_query, limit=5)
                    _log_query(
                        supplementary_query,
                        _query_sources.get(supplementary_query, "scenario"),
                        len(_stock_hits or []),
                    )
                    _merge_videos(_stock_hits)
                except Exception as error:
                    _media_pipeline()._record_discovery_failure(
                        _stock_provider, supplementary_query, error
                    )
                    print(
                        f"Stock video search failed "
                        f"for '{supplementary_query}': {error}"
                    )

            for _stock_name, _stock_provider in (
                ("search_pexels_images", "pexels"),
                ("search_pixabay_images", "pixabay"),
            ):
                try:
                    _stock_hits = _resolve_stock_provider(
                        _stock_name, globals()[_stock_name]
                    )(supplementary_query, limit=5)
                    _log_query(
                        supplementary_query,
                        _query_sources.get(supplementary_query, "scenario"),
                        len(_stock_hits or []),
                    )
                    for image in _stock_hits or []:
                        image_url = str(
                            image.get("image_url") or ""
                        ).strip()
                        if not image_url or image_url in seen_image_urls:
                            continue
                        seen_image_urls.add(image_url)
                        images.append(image)
                except Exception as error:
                    _media_pipeline()._record_discovery_failure(
                        _stock_provider, supplementary_query, error
                    )
                    print(
                        f"Stock image search failed "
                        f"for '{supplementary_query}': {error}"
                    )

        # Targeted institutional search: only when general discovery
        # found no moving footage at all for this scenario.
        if not videos:
            try:
                targeted_query = (
                    f"{primary_query} site:archive.org OR site:loc.gov "
                    f"OR site:si.edu OR site:nps.gov"
                )
                _targeted_hits = _media_pipeline().search_videos_multi(targeted_query, limit=5)
                _log_query(
                    targeted_query, "targeted_institutional",
                    len(_targeted_hits or []),
                )
                _merge_videos(_targeted_hits)
            except Exception as error:
                _media_pipeline()._record_discovery_failure("searxng-videos-targeted", primary_query, error)
                print(f"Targeted search failed for '{primary_query}': {error}")

    # Transformation Phase 4.2: institutional source hints. The
    # need's pack-derived hint queries (stamped by build_shot_needs
    # from the genre pack's source_hints templates) run on their
    # own channels — Commons category / free-text searches,
    # Archive.org advanced-search fragments, SearXNG site-scoped
    # searches — and merge into the same pools under the same
    # dedupe. Direct (T1) passes only: tier-ladder derived needs
    # may inherit the stamp but must not re-spend the hints at
    # every tier. Each hint is failure-isolated and logged under
    # source "source_hint", so the AM-021 per-query diagnostics
    # attribute its hits / candidates / verified to the hint
    # rather than to the baseline queries.
    if str(need.get("tier") or "direct") == "direct":
        for _hint in need.get("source_hint_queries") or []:
            if not isinstance(_hint, dict):
                continue
            _hint_query = " ".join(
                str(_hint.get("query") or "").split()
            ).strip()
            _hint_channel = str(_hint.get("channel") or "")
            if not _hint_query:
                continue
            try:
                _hint_hits = 0
                if _hint_channel in (
                    "wikimedia_category", "wikimedia_query",
                ):
                    _found = _media_pipeline().search_wikimedia_images(
                        _hint_query, limit=3
                    )
                    _hint_hits += len(_found or [])
                    for image in _found or []:
                        image_url = str(
                            image.get("image_url") or ""
                        ).strip()
                        if not image_url or image_url in seen_image_urls:
                            continue
                        seen_image_urls.add(image_url)
                        images.append(image)
                    _found = _media_pipeline().search_wikimedia_videos(
                        _hint_query, limit=3
                    )
                    _hint_hits += len(_found or [])
                    for video in _found or []:
                        url = str(
                            video.get("video_url")
                            or video.get("url")
                            or ""
                        ).strip()
                        if not url or url in seen_video_urls:
                            continue
                        seen_video_urls.add(url)
                        videos.append(video)
                elif _hint_channel == "archive":
                    _found = _media_pipeline().search_archive_org_videos(
                        _hint_query, limit=3
                    )
                    _hint_hits += len(_found or [])
                    for video in _found or []:
                        url = str(
                            video.get("video_url")
                            or video.get("url")
                            or ""
                        ).strip()
                        if not url or url in seen_video_urls:
                            continue
                        seen_video_urls.add(url)
                        videos.append(video)
                elif _hint_channel == "web":
                    _found = _media_pipeline().search_videos_multi(
                        _hint_query, limit=5
                    )
                    _hint_hits += len(_found or [])
                    for video in _found or []:
                        url = str(
                            video.get("video_url")
                            or video.get("url")
                            or video.get("link")
                            or ""
                        ).strip()
                        if not url or url in seen_video_urls:
                            continue
                        seen_video_urls.add(url)
                        videos.append(video)
                    _found = _media_pipeline().search_web_sources(
                        _hint_query, limit=5
                    )
                    _hint_hits += len(_found or [])
                    for source in _found or []:
                        url = str(
                            source.get("url")
                            or source.get("link")
                            or ""
                        ).strip()
                        if not url or url in seen_web_urls:
                            continue
                        seen_web_urls.add(url)
                        web_sources.append(source)
                    _found = _media_pipeline().search_image_sources(
                        _hint_query, limit=5
                    )
                    _hint_hits += len(_found or [])
                    for image in _found or []:
                        image_url = str(
                            image.get("image_url") or ""
                        ).strip()
                        if not image_url or image_url in seen_image_urls:
                            continue
                        seen_image_urls.add(image_url)
                        images.append(image)
                _log_query(_hint_query, "source_hint", _hint_hits)
            except Exception as error:
                _media_pipeline()._record_discovery_failure(
                    "source_hint", _hint_query, error
                )
                print(
                    f"Source hint search failed "
                    f"for '{_hint_query}': {error}"
                )

    return videos, web_sources, images
