"""
Persistent source-intelligence cache for the Asset Manager.

Stores, across process restarts:
  * search_cache    - Serper results keyed by normalized query
  * source_profile  - one row per YouTube/web URL (duration, chapters,
                      transcript status, failures included)
  * moment          - indexed relevant moments inside a source

The database lives OUTSIDE OneDrive (default: %LOCALAPPDATA%\\documentary-ai)
because OneDrive sync can lock SQLite files. Override with SOURCE_INTEL_DB.
"""

import contextlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

_lock = threading.RLock()
_initialized = False

_STOP = {
    "the", "a", "an", "of", "in", "on", "and", "to", "for", "with",
    "footage", "video",
}

STATS = {
    "search_hit": 0,
    "search_miss": 0,
    "serper_calls": 0,
    "serper_429": 0,
    "profile_hit": 0,
    "profile_miss": 0,
    "ytdlp_calls": 0,
    "transcript_downloads": 0,
    "yt_429": 0,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_cache (
    key TEXT PRIMARY KEY,
    endpoint TEXT,
    query_norm TEXT,
    results_json TEXT,
    fetched_at REAL,
    ttl REAL
);
CREATE INDEX IF NOT EXISTS idx_search_endpoint ON search_cache(endpoint, fetched_at);

CREATE TABLE IF NOT EXISTS source_profile (
    url TEXT PRIMARY KEY,
    status TEXT,
    duration REAL,
    title TEXT,
    channel TEXT,
    description TEXT,
    chapters_json TEXT,
    transcript_status TEXT,
    transcript_json TEXT,
    fail_reason TEXT,
    retry_after REAL,
    analyzed_at REAL
);

CREATE TABLE IF NOT EXISTS moment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT,
    start REAL,
    end REAL,
    text TEXT,
    topics_json TEXT,
    entities_json TEXT,
    confidence TEXT
);
CREATE INDEX IF NOT EXISTS idx_moment_url ON moment(url);
"""

_PROFILE_FIELDS = {
    "status", "duration", "title", "channel", "description",
    "transcript_status", "fail_reason", "retry_after",
}


# ---------------------------------------------------------------- plumbing

def _db_path() -> Path:
    override = os.getenv("SOURCE_INTEL_DB")
    if override:
        p = Path(override)
    else:
        base = os.getenv("LOCALAPPDATA") or str(Path.home())
        p = Path(base) / "documentary-ai" / "source_intel.sqlite3"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextlib.contextmanager
def _db():
    global _initialized
    with _lock:
        conn = sqlite3.connect(str(_db_path()), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            if not _initialized:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.DatabaseError:
                    pass
                conn.executescript(_SCHEMA)
                _initialized = True
            yield conn
            conn.commit()
        finally:
            conn.close()


def bump(name: str, n: int = 1) -> None:
    STATS[name] = STATS.get(name, 0) + n


def stats_summary() -> dict:
    return dict(STATS)


def reset_stats() -> None:
    for k in list(STATS):
        STATS[k] = 0


# ---------------------------------------------------------------- queries

def normalize_query(q: str) -> str:
    toks = [
        t for t in re.findall(r"[a-z0-9]+", (q or "").lower())
        if t not in _STOP and len(t) > 1
    ]
    if not toks:
        return (q or "").strip().lower()
    return " ".join(sorted(set(toks)))


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def search_get(endpoint: str, query: str, near_threshold: float = 0.8) -> Optional[list]:
    """Exact or near-duplicate cached results, else None."""
    norm = normalize_query(query)
    now = time.time()
    with _db() as c:
        row = c.execute(
            "SELECT * FROM search_cache WHERE key=?", (f"{endpoint}::{norm}",)
        ).fetchone()
        if row and now - row["fetched_at"] < row["ttl"]:
            bump("search_hit")
            return json.loads(row["results_json"])

        rows = c.execute(
            "SELECT query_norm, results_json, fetched_at, ttl FROM search_cache "
            "WHERE endpoint=? ORDER BY fetched_at DESC LIMIT 500",
            (endpoint,),
        ).fetchall()
        for r in rows:
            if now - r["fetched_at"] < r["ttl"] and _jaccard(norm, r["query_norm"]) >= near_threshold:
                bump("search_hit")
                return json.loads(r["results_json"])

    bump("search_miss")
    return None


def search_put(endpoint: str, query: str, results: list) -> None:
    norm = normalize_query(query)
    ttl = 30 * 86400 if results else 2 * 86400
    with _db() as c:
        c.execute(
            "INSERT OR REPLACE INTO search_cache VALUES (?,?,?,?,?,?)",
            (f"{endpoint}::{norm}", endpoint, norm, json.dumps(results), time.time(), ttl),
        )


# ---------------------------------------------------------------- profiles

def profile_get(url: str) -> Optional[dict]:
    """
    Cached profile for a URL, or None. A rate_limited transcript whose
    cooldown has passed is reported as 'unknown' so the caller may retry
    it, while duration/chapters stay cached.
    """
    with _db() as c:
        row = c.execute("SELECT * FROM source_profile WHERE url=?", (url,)).fetchone()
    if not row:
        bump("profile_miss")
        return None

    p = dict(row)
    p["chapters"] = json.loads(p.pop("chapters_json") or "[]")
    p["transcript"] = json.loads(p.pop("transcript_json") or "[]")
    if p["transcript_status"] == "rate_limited" and (p["retry_after"] or 0) < time.time():
        p["transcript_status"] = "unknown"
    bump("profile_hit")
    return p


def profile_upsert(url: str, **fields) -> None:
    """Merge fields into the stored profile (creating it if needed)."""
    with _db() as c:
        row = c.execute("SELECT * FROM source_profile WHERE url=?", (url,)).fetchone()
        cur = dict(row) if row else {
            "url": url, "status": "ok", "duration": None, "title": "",
            "channel": "", "description": "", "chapters_json": "[]",
            "transcript_status": "unknown", "transcript_json": "[]",
            "fail_reason": "", "retry_after": 0.0,
        }
        for k, v in fields.items():
            if k == "chapters":
                cur["chapters_json"] = json.dumps(v or [])
            elif k == "transcript":
                cur["transcript_json"] = json.dumps(v or [])
            elif k in _PROFILE_FIELDS:
                cur[k] = v
        cur["analyzed_at"] = time.time()
        c.execute(
            "INSERT OR REPLACE INTO source_profile "
            "(url,status,duration,title,channel,description,chapters_json,"
            "transcript_status,transcript_json,fail_reason,retry_after,analyzed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                url, cur["status"], cur["duration"], cur["title"], cur["channel"],
                cur["description"], cur["chapters_json"], cur["transcript_status"],
                cur["transcript_json"], cur["fail_reason"], cur["retry_after"],
                cur["analyzed_at"],
            ),
        )


# ---------------------------------------------------------------- moments

def moments_replace(url: str, moments: list) -> None:
    with _db() as c:
        c.execute("DELETE FROM moment WHERE url=?", (url,))
        c.executemany(
            "INSERT INTO moment (url,start,end,text,topics_json,entities_json,confidence) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (
                    url, m["start"], m["end"], m.get("text", ""),
                    json.dumps(m.get("topics", [])),
                    json.dumps(m.get("entities", [])),
                    m.get("confidence", "high"),
                )
                for m in moments
            ],
        )


def moments_for(url: str) -> list:
    with _db() as c:
        rows = c.execute("SELECT * FROM moment WHERE url=? ORDER BY start", (url,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["topics"] = json.loads(d.pop("topics_json"))
        d["entities"] = json.loads(d.pop("entities_json"))
        out.append(d)
    return out


# ---------------------------------------------------------------- YouTube 429 breaker

_yt_block_until = 0.0


def yt_breaker_trip(seconds: float = 90.0) -> None:
    """Call when YouTube returns HTTP 429. Pauses all YouTube calls."""
    global _yt_block_until
    bump("yt_429")
    _yt_block_until = max(_yt_block_until, time.time() + seconds)


def yt_breaker_remaining() -> float:
    return max(0.0, _yt_block_until - time.time())


def yt_breaker_open() -> bool:
    """True while YouTube calls should be skipped."""
    return yt_breaker_remaining() > 0