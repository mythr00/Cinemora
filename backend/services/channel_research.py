"""YouTube channel research (Channel Research directive,
2026-11-26), integrated into Script Generation and the
Thumbnail Studio.

REAL DATA ONLY. Everything in a research record comes from
yt-dlp's extraction of the public channel the user supplied:
titles, view counts, durations, upload dates, thumbnails,
descriptions and — when the video actually exposes subtitles —
transcript text. Nothing is estimated, modelled or invented:
when a statistic is absent from the extraction it is absent
from the record, and the analysis is labelled per video by what
was actually accessible (transcript vs metadata only).

Flow (the directive's): channel URL → the channel's 10 most
relevant successful videos → the user selects at most 3 → a
pattern analysis grounded ONLY in the fetched data → original
idea proposals → the EXISTING script generation (no second
pipeline) → the existing production handoff.

Records are per-user JSON beside the Script Studio store
(users/<id>/channel_research.json).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from services import llm_provider, media_paths

STORE_FILENAME = "channel_research.json"
MAX_VIDEOS = 10
MAX_SELECTED = 3
TRANSCRIPT_CHAR_CAP = 12_000
_YTDLP_TIMEOUT = 90

_CHANNEL_URL = re.compile(
    r"^https?://(www\.)?youtube\.com/"
    r"(@[A-Za-z0-9._-]+|channel/UC[A-Za-z0-9_-]+|c/[A-Za-z0-9_-]+|user/[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

# Trailing channel-page tabs users copy from YouTube's own UI —
# stripped during normalisation so the /videos listing is read.
_CHANNEL_TABS = {
    "videos", "shorts", "streams", "featured", "about",
    "community", "playlists", "live",
}


def normalise_channel_url(raw: str) -> str:
    """Canonicalise the channel addresses users actually paste:
    a bare @handle, a scheme-less host, the mobile host, tracking
    query strings, and URLs that already carry a channel tab
    (…/@Handle/videos is the address YouTube itself shows).
    Returns the canonical channel root; raises nothing — the
    caller validates the result against _CHANNEL_URL."""
    url = str(raw or "").strip()
    if not url:
        return url
    if url.startswith("@"):
        url = f"https://www.youtube.com/{url}"
    elif "://" not in url:
        url = f"https://{url}"
    # Drop query/fragment (share trackers, ?si=…, tab params).
    url = url.split("?", 1)[0].split("#", 1)[0]
    url = re.sub(
        r"^(https?)://m\.youtube\.com", r"\1://www.youtube.com",
        url, flags=re.IGNORECASE,
    )
    url = re.sub(
        r"^(https?)://youtube\.com", r"\1://www.youtube.com",
        url, flags=re.IGNORECASE,
    )
    url = url.rstrip("/")
    # Strip trailing channel tabs (possibly repeated).
    while True:
        head, _, last = url.rpartition("/")
        if last.lower() in _CHANNEL_TABS and _CHANNEL_URL.match(head):
            url = head
        else:
            break
    return url


class ChannelResearchError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _user_dir(user_id) -> Path:
    return media_paths.media_root() / "users" / str(user_id)


def _load_store(user_id) -> dict:
    path = _user_dir(user_id) / STORE_FILENAME
    if not path.is_file():
        return {"records": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"records": []}
    if not isinstance(data, dict) or not isinstance(
        data.get("records"), list
    ):
        return {"records": []}
    return data


def _save_store(user_id, store: dict) -> None:
    directory = _user_dir(user_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / STORE_FILENAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=1), encoding="utf-8")
    tmp.replace(path)


def _find_record(store: dict, research_id: str) -> dict:
    for record in store.get("records") or []:
        if record.get("id") == research_id:
            return record
    raise ChannelResearchError(
        404, "research_not_found", "No such channel research record."
    )


# ---------------------------------------------------------------------
# yt-dlp extraction (public data only; the pipeline's TLS-safe
# base command, so behaviour matches the rest of the system)
# ---------------------------------------------------------------------


def _run_ytdlp(args: list[str]) -> dict:
    from services import media_pipeline

    command = [
        *media_pipeline._yt_dlp_base_command(),
        "--no-warnings",
        "--socket-timeout", "20",
        *args,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_YTDLP_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise ChannelResearchError(
            502, "channel_fetch_failed",
            "Reading the channel timed out; nothing was recorded.",
        ) from error
    except OSError as error:
        raise ChannelResearchError(
            502, "channel_fetch_failed",
            f"yt-dlp could not run: {error}",
        ) from error
    if completed.returncode != 0:
        stderr = completed.stderr or ""
        detail = stderr.strip().splitlines()
        tail = detail[-1] if detail else "unknown error"
        # Known environment failures get an actionable message
        # instead of a raw yt-dlp stderr tail.
        if "CERTIFICATE_VERIFY_FAILED" in stderr or "self-signed" in stderr:
            raise ChannelResearchError(
                502, "channel_fetch_failed",
                "The secure connection to YouTube could not be "
                "verified from this deployment, so the channel "
                "could not be read. Nothing was recorded.",
            )
        if "Sign in to confirm" in stderr or "not a bot" in stderr:
            raise ChannelResearchError(
                502, "channel_fetch_failed",
                "YouTube refused the read — it is treating this "
                "server as automated traffic. Nothing was "
                "recorded; trying again later usually works.",
            )
        raise ChannelResearchError(
            502, "channel_fetch_failed",
            f"YouTube could not be read for that URL ({tail}). "
            "Nothing was recorded.",
        )
    try:
        return json.loads(completed.stdout)
    except ValueError as error:
        raise ChannelResearchError(
            502, "channel_fetch_failed",
            "YouTube returned unreadable data; nothing was recorded.",
        ) from error


def _video_view(entry: dict) -> dict:
    return {
        "id": entry.get("id"),
        "title": entry.get("title"),
        "url": entry.get("url")
        or (
            f"https://www.youtube.com/watch?v={entry['id']}"
            if entry.get("id")
            else None
        ),
        "view_count": entry.get("view_count"),
        "like_count": entry.get("like_count"),
        "duration_seconds": entry.get("duration"),
        "upload_date": entry.get("upload_date"),
        "thumbnail": entry.get("thumbnail")
        or (
            (entry.get("thumbnails") or [{}])[-1].get("url")
            if entry.get("thumbnails")
            else None
        ),
    }


def fetch_channel(channel_url: str) -> dict:
    """Resolve a channel URL to {channel, videos, ordering_basis}
    from a flat playlist extraction. Videos are the channel's
    uploads, ordered by view count when the extraction reports
    counts (the "most successful" reading), else in the order
    YouTube returned them — the basis is stated, never hidden."""
    url = normalise_channel_url(channel_url)
    if not _CHANNEL_URL.match(url):
        raise ChannelResearchError(
            422, "channel_url_invalid",
            "That does not look like a YouTube channel URL "
            "(a @handle, /channel/, /c/ or /user/ address).",
        )
    videos_url = url + "/videos"
    data = _run_ytdlp(
        [
            "--flat-playlist",
            "--dump-single-json",
            "--playlist-end", "30",
            videos_url,
        ]
    )
    entries = [
        entry
        for entry in (data.get("entries") or [])
        if isinstance(entry, dict) and entry.get("id")
    ]
    if not entries:
        raise ChannelResearchError(
            404, "channel_empty",
            "That channel URL resolved, but no public videos were "
            "found on it.",
        )
    with_counts = [e for e in entries if e.get("view_count") is not None]
    if len(with_counts) >= 3:
        ordered = sorted(
            entries,
            key=lambda e: e.get("view_count") or 0,
            reverse=True,
        )
        basis = "view_count_desc"
    else:
        ordered = entries
        basis = "channel_order"
    channel = {
        "id": data.get("channel_id") or data.get("id"),
        "title": data.get("channel") or data.get("title"),
        "url": data.get("channel_url") or url,
        "subscriber_count": data.get("channel_follower_count"),
    }
    return {
        "channel": channel,
        "videos": [_video_view(e) for e in ordered[:MAX_VIDEOS]],
        "ordering_basis": basis,
    }


def fetch_video_detail(video_id: str) -> dict:
    """Full extraction for one selected video + a transcript
    attempt. basis is "transcript" only when subtitle text was
    actually retrieved; otherwise "metadata"."""
    data = _run_ytdlp(
        [
            "--dump-single-json",
            "--skip-download",
            f"https://www.youtube.com/watch?v={video_id}",
        ]
    )
    detail = _video_view(data)
    detail["description"] = (data.get("description") or "")[:4000]
    detail["tags"] = (data.get("tags") or [])[:20]
    transcript = _fetch_transcript(video_id)
    detail["transcript"] = transcript
    detail["basis"] = "transcript" if transcript else "metadata"
    return detail


def _fetch_transcript(video_id: str) -> str | None:
    from services import media_pipeline

    with tempfile.TemporaryDirectory(prefix="cinemora-subs-") as tmp:
        command = [
            *media_pipeline._yt_dlp_base_command(),
            "--no-warnings",
            "--socket-timeout", "20",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", "en.*",
            "--sub-format", "vtt",
            "-o", str(Path(tmp) / "%(id)s.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        try:
            subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_YTDLP_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        candidates = sorted(Path(tmp).glob("*.vtt"))
        if not candidates:
            return None
        text = _vtt_to_text(
            candidates[0].read_text(encoding="utf-8", errors="replace")
        )
        return text[:TRANSCRIPT_CHAR_CAP] if text else None


_VTT_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3} -->")
_VTT_TAG = re.compile(r"<[^>]+>")


def _vtt_to_text(vtt: str) -> str:
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if (
            not line
            or line.startswith(("WEBVTT", "Kind:", "Language:"))
            or _VTT_TIME.match(line)
            or line.isdigit()
        ):
            continue
        line = _VTT_TAG.sub("", line)
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return " ".join(lines).strip()


# ---------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------


def resolve_channel(user_id, channel_url: str) -> dict:
    fetched = fetch_channel(channel_url)
    record = {
        "id": f"research-{uuid.uuid4().hex[:12]}",
        "channel": fetched["channel"],
        "videos": fetched["videos"],
        "ordering_basis": fetched["ordering_basis"],
        "analysis": None,
        "created_at": _now_iso(),
    }
    store = _load_store(user_id)
    store["records"].insert(0, record)
    del store["records"][20:]
    _save_store(user_id, store)
    return record


def get_research(user_id, research_id: str) -> dict:
    return _find_record(_load_store(user_id), research_id)


def list_research(user_id) -> dict:
    store = _load_store(user_id)
    return {"records": store.get("records") or []}


def analyse_research(user_id, research_id: str, video_ids) -> dict:
    """Detail-fetch the selected videos (≤3), then ONE analysis
    call grounded only in the fetched data. The analysis record
    states, per video, whether it rests on the transcript or on
    metadata alone."""
    store = _load_store(user_id)
    record = _find_record(store, research_id)
    selected_ids = list(
        dict.fromkeys(str(v) for v in (video_ids or []) if str(v).strip())
    )
    if not selected_ids:
        raise ChannelResearchError(
            422, "selection_required",
            "Select at least one video to analyse.",
        )
    if len(selected_ids) > MAX_SELECTED:
        raise ChannelResearchError(
            422, "too_many_selected",
            f"Select at most {MAX_SELECTED} videos to analyse.",
        )
    known = {video["id"] for video in record.get("videos") or []}
    unknown = [v for v in selected_ids if v not in known]
    if unknown:
        raise ChannelResearchError(
            422, "selection_unknown",
            "A selected video is not part of this research record.",
        )
    # The per-video extractions are independent — run them
    # concurrently so analysing 3 videos takes roughly one
    # video's time, not three in sequence inside one request.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(selected_ids)) as pool:
        details = list(pool.map(fetch_video_detail, selected_ids))
    analysis = _analyse_with_llm(user_id, record, details)
    record["analysis"] = {
        "at": _now_iso(),
        "basis": {d["id"]: d["basis"] for d in details},
        "videos": [
            {
                "id": d["id"],
                "title": d["title"],
                "basis": d["basis"],
                "view_count": d["view_count"],
                "duration_seconds": d["duration_seconds"],
            }
            for d in details
        ],
        "patterns": analysis["patterns"],
        "ideas": analysis["ideas"],
    }
    _save_store(user_id, store)
    return record


def _analyse_with_llm(user_id, record: dict, details: list[dict]) -> dict:
    if not llm_provider.is_configured():
        raise ChannelResearchError(
            503, "analysis_unavailable",
            "Pattern analysis needs the AI provider, which is not "
            "configured on this deployment. The channel data above "
            "is real and stays recorded.",
        )
    evidence = []
    for detail in details:
        evidence.append(
            {
                "title": detail["title"],
                "view_count": detail["view_count"],
                "like_count": detail["like_count"],
                "duration_seconds": detail["duration_seconds"],
                "upload_date": detail["upload_date"],
                "basis": detail["basis"],
                "description_excerpt": (detail["description"] or "")[:800],
                "transcript_excerpt": (detail["transcript"] or "")[:3000]
                or None,
            }
        )
    prompt = (
        f"Channel: {record['channel'].get('title')}\n\n"
        "Evidence (the ONLY facts you may use — real extraction "
        "data; a video's basis says whether a transcript was "
        "actually available or only metadata):\n"
        + json.dumps(evidence, indent=1)
        + "\n\nAnalyse the patterns behind these videos' "
        "performance: titles, hooks, structure, pacing, duration, "
        "subject choices — only where the evidence supports the "
        "claim, and say which basis each pattern rests on. Then "
        "propose ORIGINAL documentary ideas for a DIFFERENT "
        "channel that apply the patterns without copying this "
        "channel's subjects, scripts or distinctive expression.\n\n"
        'Return JSON: {"patterns": [{"pattern": str, "basis": '
        '"transcript"|"metadata", "evidence": str}], "ideas": '
        '[{"title": str, "angle": str, "why_it_fits": str}]}'
    )
    try:
        result = llm_provider.complete_json(
            "You analyse documentary channels from real evidence. "
            "You never invent statistics, and you never propose "
            "copying another creator's work.",
            prompt,
            max_tokens=3000,
            usage_context={
                "user_id": user_id,
                "context": "channel_research_analysis",
            },
        )
    except Exception as error:
        raise ChannelResearchError(
            502, "analysis_failed",
            f"The pattern analysis failed ({type(error).__name__}); "
            "the channel data stays recorded.",
        ) from error
    patterns = (result or {}).get("patterns")
    ideas = (result or {}).get("ideas")
    if not isinstance(patterns, list) or not isinstance(ideas, list):
        raise ChannelResearchError(
            502, "analysis_failed",
            "The pattern analysis returned an unusable shape; the "
            "channel data stays recorded.",
        )
    return {
        # Patterns are coerced to the documented shape (the ideas
        # below already are): a malformed provider item must never
        # reach the frontend raw and crash its rendering.
        "patterns": [
            {
                "pattern": str(item.get("pattern") or "")[:500],
                "basis": str(item.get("basis") or "metadata")[:40],
                "evidence": str(item.get("evidence") or "")[:1000],
            }
            for item in patterns[:10]
            if isinstance(item, dict) and item.get("pattern")
        ],
        "ideas": [
            {
                "id": f"idea-{index}",
                "title": str(idea.get("title") or "")[:300],
                "angle": str(idea.get("angle") or "")[:1000],
                "why_it_fits": str(idea.get("why_it_fits") or "")[:1000],
            }
            for index, idea in enumerate(ideas[:6])
            if isinstance(idea, dict) and idea.get("title")
        ],
    }


# ---------------------------------------------------------------------
# Channel thumbnail style measurement (Thumbnail Studio, item 5)
# ---------------------------------------------------------------------


def channel_thumbnail_styles(channel_url: str, *, limit: int = 6) -> dict:
    """REAL channel data + MEASURED thumbnail properties: the
    channel's top videos (same extraction as research) with each
    thumbnail's measured palette / OCR text / luminance, via the
    Thumbnail Studio's own measurement core. These measurements
    inform the user's own original designs — the channel's
    thumbnails themselves are never usable as composition bases
    (they are another creator's artwork)."""
    import requests

    from services import thumbnail_studio

    fetched = fetch_channel(channel_url)
    measured = []
    for video in fetched["videos"][:limit]:
        entry = {**video, "measurement": None, "measurement_error": None}
        thumbnail_url = video.get("thumbnail")
        if thumbnail_url:
            try:
                response = requests.get(
                    thumbnail_url,
                    timeout=20,
                    headers={"User-Agent": "Cinemora/1.0"},
                )
                response.raise_for_status()
                data = response.content
                if len(data) > 5_000_000:
                    raise ValueError("thumbnail larger than 5 MB")
                with tempfile.TemporaryDirectory(
                    prefix="cinemora-thumb-"
                ) as tmp:
                    path = Path(tmp) / "thumb.jpg"
                    path.write_bytes(data)
                    entry["measurement"] = (
                        thumbnail_studio.measure_image_file(path)
                    )
            except Exception as error:  # measured or honestly absent
                entry["measurement_error"] = (
                    f"{type(error).__name__}: measurement unavailable"
                )
        measured.append(entry)
    return {
        "channel": fetched["channel"],
        "ordering_basis": fetched["ordering_basis"],
        "videos": measured,
        "label": (
            "Measured properties of this channel's public "
            "thumbnails (palette, OCR text, luminance) — style "
            "reference only. These images are the channel's own "
            "artwork and cannot be used as bases for your "
            "thumbnails."
        ),
    }
