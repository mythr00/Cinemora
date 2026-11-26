"""Durable per-production discovery memory (Phase 2 — Discovery Efficiency).

Why this module exists
----------------------
Before Phase 2, every piece of asset-discovery state — attempted
URLs, rejections and their reasons, download failures, verification
verdicts — lived in per-run in-memory globals (``media_pipeline.py``).
A new run of the SAME production therefore started from amnesia: it
re-downloaded candidates already known dead and re-verified
candidates already judged, at full search / download / LLM cost.
The Hyatt production made the waste measurable: 58.2% of candidate
evaluations were repeats of evaluations an earlier run (or an
earlier scene of the same run) had already paid for.

This module is the durable store the pipeline consults across
scenes, runs and process restarts. It is per-PRODUCTION (per
project): one store per project directory, in a SQLite file named
``discovery_memory.sqlite3`` inside that directory, so the memory
travels with the production's artefacts and dies with them — it is
never shared across productions. That scoping matters for honesty
as well as cost: a verdict recorded against one production's script
and case bible must never silently judge a candidate in another
production (see the verdict expiry rule below).

Fail-soft contract (hard requirement)
-------------------------------------
Following ``services.stage_resume``: memory is an optimisation and
an honesty record, never a new failure mode for a production. No
public method ever raises. Any SQLite / OS / serialisation error
turns the operation into a no-op (writes) or a miss (lookups return
``None``). A production whose discovery memory is corrupt, locked,
deleted mid-flight or on a read-only filesystem runs exactly as it
did before Phase 2 — it just pays the old repeat costs.

Expiry rules (stated here because they are semantics, not detail)
-----------------------------------------------------------------
* Download failures: a failure recorded as ``retryable`` (transient
  — timeouts, network errors, HTTP 429/5xx) expires
  ``TRANSIENT_FAILURE_TTL_SECONDS`` (24h) after its ``last_seen``
  and is then treated as absent (lazily deleted on read), because a
  transient cause may well have cleared. A deterministic failure
  (``retryable=False`` — 404/410, rights, verification) never
  expires within the production's life: the fact it records does
  not heal with time.
* Verdicts: a verdict is valid only for the exact context it was
  judged in. The lookup key is (candidate, context fingerprint,
  case-bible fingerprint, ``VERDICT_CODE_VERSION``), so a changed
  script (new bible), a changed context, or a changed verdict code
  version is a miss BY CONSTRUCTION. Within those bounds verdicts
  never expire by time — re-judging an unchanged candidate in an
  unchanged production is precisely the waste this store removes.
* Intrinsic verdicts: verdicts whose rejection reason is intrinsic
  to the candidate and independent of context (banned provider,
  foreign provenance, social platform, meme format) are recorded
  separately, are bible-independent, and never expire. The CALLER
  decides a verdict is intrinsic and only then calls
  ``record_intrinsic_verdict``; this store does not guess.
* Demotions (post-download rejections) and sentence states never
  expire by time; a demotion lookup with a different context
  fingerprint is a miss, for the same reason verdicts are keyed
  by context.

First evaluation wins for verdict items: a repeat record for an
existing key updates ``last_seen``/``hits`` but keeps the FIRST
stored item, mirroring the ledger's attempt records — the first
judgement is the evidence; later repeats are only proof it was
consulted again.
"""

import copy
import json
import sqlite3
import threading
import time
from pathlib import Path

#: On-disk schema version, stored in the ``meta`` table. A store
#: whose recorded version does not match is treated as empty (its
#: tables are recreated) rather than raising or being migrated
#: speculatively: discovery memory is reconstructible by simply
#: re-running discovery, so a destructive reset loses cost, never
#: correctness.
SCHEMA_VERSION = 1

#: Version of the verdict-evaluation code/semantics that produced
#: the stored verdict items. Bump when the meaning of a verdict
#: changes; old rows become unreachable by construction because
#: every verdict lookup keys on the CURRENT value.
#: "2" (Phase 3): verify_candidate gained the illustrative-stock
#: vs direct-evidence rule — a verdict recorded under "1" may
#: accept an illustrative candidate for an evidence need that "2"
#: rejects, so "1" verdicts must never be served again.
VERDICT_CODE_VERSION = "2"

#: How long a retryable (transient) download failure is trusted
#: after it was last seen. See the expiry rules in the module
#: docstring.
TRANSIENT_FAILURE_TTL_SECONDS = 24 * 3600

_DB_FILENAME = "discovery_memory.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS download_failures (
    scope TEXT,
    key TEXT,
    failure_class TEXT,
    retryable INTEGER,
    reason TEXT,
    attempts INTEGER,
    first_seen REAL,
    last_seen REAL,
    PRIMARY KEY(scope, key)
);
CREATE TABLE IF NOT EXISTS download_successes (
    dl_key TEXT PRIMARY KEY,
    content_hash TEXT,
    sentence_number INTEGER,
    seen REAL
);
CREATE TABLE IF NOT EXISTS verdicts (
    candidate_key TEXT,
    context_fp TEXT,
    bible_fp TEXT,
    code_version TEXT,
    item_json TEXT,
    accepted INTEGER,
    rejection_reason TEXT,
    rejection_stage TEXT,
    sentence_numbers TEXT,
    first_seen REAL,
    last_seen REAL,
    hits INTEGER,
    PRIMARY KEY(candidate_key, context_fp, bible_fp, code_version)
);
CREATE TABLE IF NOT EXISTS intrinsic_verdicts (
    candidate_key TEXT PRIMARY KEY,
    item_json TEXT,
    rejection_reason TEXT,
    first_seen REAL,
    last_seen REAL,
    hits INTEGER
);
CREATE TABLE IF NOT EXISTS demotions (
    candidate_key TEXT,
    segment_key TEXT,
    context_fp TEXT,
    stage TEXT,
    reason TEXT,
    first_seen REAL,
    last_seen REAL,
    hits INTEGER,
    PRIMARY KEY(candidate_key, segment_key, context_fp)
);
CREATE TABLE IF NOT EXISTS sentence_states (
    sentence_number INTEGER PRIMARY KEY,
    state TEXT,
    detail TEXT,
    updated REAL
);
CREATE TABLE IF NOT EXISTS evidence (
    candidate_id TEXT,
    sentence_number INTEGER,
    scene_number INTEGER,
    beat_id TEXT,
    url TEXT,
    provider TEXT,
    kind TEXT,
    tier TEXT,
    classification TEXT,
    score REAL,
    outcome TEXT,
    content_hash TEXT,
    segment_id TEXT,
    duplicate_of TEXT,
    recorded_at REAL,
    PRIMARY KEY(candidate_id, sentence_number)
);
"""

_TABLES = (
    "download_failures",
    "download_successes",
    "verdicts",
    "intrinsic_verdicts",
    "demotions",
    "sentence_states",
    "evidence",
)


def _blank(value) -> bool:
    """True for a key that cannot identify anything (None/blank)."""
    return not isinstance(value, str) or not value.strip()


class DiscoveryMemory:
    """One durable discovery-memory store for one project directory.

    Use :meth:`open` — it caches one instance per absolute project
    directory, so every caller in the process shares the same
    connection and lock for a given production. Direct construction
    is supported (tests use it for injected clocks) but bypasses
    the cache.
    """

    _cache: dict = {}
    _cache_lock = threading.Lock()

    def __init__(self, project_dir, clock=None):
        self._clock = clock if callable(clock) else time.time
        self._lock = threading.Lock()
        self._conn = None
        self._closed = False
        self._project_dir = None
        self._db_path = None
        try:
            base = Path(project_dir)
            if not base.is_dir():
                # Unusable directory (missing, or a plain file):
                # leave the instance inert — every method no-ops.
                return
            self._project_dir = base
            self._db_path = base / _DB_FILENAME
            self._connect()
        except Exception:
            self._conn = None

    # ------------------------------------------------------------ plumbing

    def _connect(self):
        """Open the SQLite connection and (re)create the schema.

        Raises on failure; callers in ``__init__``/``open`` convert
        that into an inert instance / ``None``."""
        conn = sqlite3.connect(
            str(self._db_path), timeout=5.0, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        try:
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.DatabaseError:
            pass
        self._init_schema(conn)
        self._conn = conn

    def _init_schema(self, conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        version = None
        if row is not None:
            try:
                version = int(row[0])
            except (TypeError, ValueError):
                version = None
        if version is not None and version != SCHEMA_VERSION:
            # Mismatched on-disk version: treat the store as empty
            # and recreate its tables (see SCHEMA_VERSION note).
            for table in _TABLES + ("meta",):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()

    def _now(self) -> float:
        try:
            return float(self._clock())
        except Exception:
            return time.time()

    def _read_conn(self):
        """Connection for a lookup, or None when the store is
        unusable — including when the database file was deleted
        mid-flight, in which case lookups honestly answer None
        instead of resurrecting (or erroring on) a detached file."""
        if self._closed or self._conn is None or self._db_path is None:
            return None
        try:
            if not self._db_path.exists():
                return None
        except OSError:
            return None
        return self._conn

    def _write_conn(self):
        """Connection for a write, or None. Unlike reads, a write
        after the file was deleted mid-flight tries once to
        reconnect (recreating a fresh, empty store) — losing the
        old memory is acceptable, losing the new record is not
        necessary."""
        if self._closed or self._db_path is None:
            return None
        if self._conn is not None:
            try:
                if self._db_path.exists():
                    return self._conn
            except OSError:
                pass
            # File vanished under the open connection: drop it and
            # fall through to a single reconnect attempt.
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        try:
            if self._project_dir is None or not self._project_dir.is_dir():
                return None
            self._connect()
        except Exception:
            self._conn = None
            return None
        return self._conn

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def open(cls, project_dir, clock=None):
        """The cached store for ``project_dir``, or None when the
        directory is unusable (missing, not a directory, or the
        database cannot be created). Never raises."""
        try:
            if project_dir is None:
                return None
            key = str(Path(project_dir).resolve())
            if not Path(project_dir).is_dir():
                return None
        except Exception:
            return None
        with cls._cache_lock:
            cached = cls._cache.get(key)
            if cached is not None and not cached._closed:
                return cached
            try:
                instance = cls(project_dir, clock=clock)
            except Exception:
                return None
            if instance._conn is None:
                return None
            cls._cache[key] = instance
            return instance

    @classmethod
    def close_all(cls):
        """Close every cached instance and clear the cache."""
        with cls._cache_lock:
            instances = list(cls._cache.values())
            cls._cache.clear()
        for instance in instances:
            try:
                instance.close()
            except Exception:
                pass

    def close(self):
        """Close this store and drop it from the instance cache."""
        try:
            with DiscoveryMemory._cache_lock:
                for key, cached in list(DiscoveryMemory._cache.items()):
                    if cached is self:
                        DiscoveryMemory._cache.pop(key, None)
        except Exception:
            pass
        with self._lock:
            self._closed = True
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # -------------------------------------------------- download failures

    def record_download_failure(self, scope, key, *, failure_class=None,
                                retryable=False, reason=None):
        """Record (or re-record) a download failure.

        Re-recording increments ``attempts``, refreshes
        ``last_seen``, and lets the latest class / retryable flag /
        reason win — the most recent observation is the best
        description of why this download fails. Returns True when
        recorded, False on a blank key or any store error."""
        if _blank(scope) or _blank(key):
            return False
        try:
            with self._lock:
                conn = self._write_conn()
                if conn is None:
                    return False
                now = self._now()
                conn.execute(
                    "INSERT INTO download_failures"
                    "(scope, key, failure_class, retryable, reason,"
                    " attempts, first_seen, last_seen)"
                    " VALUES (?,?,?,?,?,1,?,?)"
                    " ON CONFLICT(scope, key) DO UPDATE SET"
                    " failure_class=excluded.failure_class,"
                    " retryable=excluded.retryable,"
                    " reason=excluded.reason,"
                    " attempts=download_failures.attempts+1,"
                    " last_seen=excluded.last_seen",
                    (scope, key, failure_class, 1 if retryable else 0,
                     reason, now, now),
                )
                conn.commit()
                return True
        except Exception:
            return False

    def download_failure(self, scope, key):
        """The recorded failure for (scope, key), or None.

        Expiry rule: a retryable (transient) failure older than
        ``TRANSIENT_FAILURE_TTL_SECONDS`` since ``last_seen`` is
        treated as absent and lazily deleted; a deterministic
        failure never expires within the production's life."""
        if _blank(scope) or _blank(key):
            return None
        try:
            with self._lock:
                conn = self._read_conn()
                if conn is None:
                    return None
                row = conn.execute(
                    "SELECT * FROM download_failures WHERE scope=? AND key=?",
                    (scope, key),
                ).fetchone()
                if row is None:
                    return None
                result = {
                    "failure_class": row["failure_class"],
                    "retryable": bool(row["retryable"]),
                    "reason": row["reason"],
                    "attempts": int(row["attempts"] or 0),
                    "first_seen": row["first_seen"],
                    "last_seen": row["last_seen"],
                }
                if result["retryable"] and (
                    self._now() - float(result["last_seen"] or 0)
                    > TRANSIENT_FAILURE_TTL_SECONDS
                ):
                    # Expired transient failure: lazily delete it
                    # (best-effort; the read already has its answer).
                    try:
                        write = self._write_conn()
                        if write is not None:
                            write.execute(
                                "DELETE FROM download_failures"
                                " WHERE scope=? AND key=?",
                                (scope, key),
                            )
                            write.commit()
                    except Exception:
                        pass
                    return None
                return result
        except Exception:
            return None

    def iter_download_failures(self):
        """All live download-failure records as (scope, key, record)
        tuples — the seeding surface a new run uses to repopulate
        its in-run failure guards without re-asking per key. The
        same expiry rule as download_failure() applies: expired
        transient failures are omitted. Fail-soft: []."""
        try:
            with self._lock:
                conn = self._read_conn()
                if conn is None:
                    return []
                rows = conn.execute(
                    "SELECT * FROM download_failures"
                ).fetchall()
                now = self._now()
                out = []
                for row in rows:
                    record = {
                        "failure_class": row["failure_class"],
                        "retryable": bool(row["retryable"]),
                        "reason": row["reason"],
                        "attempts": int(row["attempts"] or 0),
                        "first_seen": row["first_seen"],
                        "last_seen": row["last_seen"],
                    }
                    if record["retryable"] and (
                        now - float(record["last_seen"] or 0)
                        > TRANSIENT_FAILURE_TTL_SECONDS
                    ):
                        continue
                    out.append((row["scope"], row["key"], record))
                return out
        except Exception:
            return []

    def record_download_success(self, dl_key, *, content_hash=None,
                                sentence_number=None):
        """Record a successful download and supersede its failure.

        A success deletes any ``download_failures`` row with
        scope="download_url" for the same key: an earlier transient
        failure of a URL that has since downloaded is stale news,
        and keeping it would make later runs skip a working source
        (mirrors the in-run dispatcher behaviour). Returns True
        when recorded."""
        if _blank(dl_key):
            return False
        try:
            with self._lock:
                conn = self._write_conn()
                if conn is None:
                    return False
                conn.execute(
                    "INSERT INTO download_successes"
                    "(dl_key, content_hash, sentence_number, seen)"
                    " VALUES (?,?,?,?)"
                    " ON CONFLICT(dl_key) DO UPDATE SET"
                    " content_hash=excluded.content_hash,"
                    " sentence_number=excluded.sentence_number,"
                    " seen=excluded.seen",
                    (dl_key, content_hash, sentence_number, self._now()),
                )
                conn.execute(
                    "DELETE FROM download_failures"
                    " WHERE scope='download_url' AND key=?",
                    (dl_key,),
                )
                conn.commit()
                return True
        except Exception:
            return False

    def download_success(self, dl_key):
        """The recorded success for a download-URL key, or None."""
        if _blank(dl_key):
            return None
        try:
            with self._lock:
                conn = self._read_conn()
                if conn is None:
                    return None
                row = conn.execute(
                    "SELECT * FROM download_successes WHERE dl_key=?",
                    (dl_key,),
                ).fetchone()
                if row is None:
                    return None
                return {
                    "content_hash": row["content_hash"],
                    "sentence_number": row["sentence_number"],
                    "seen": row["seen"],
                }
        except Exception:
            return None

    # ------------------------------------------------------------ verdicts

    def record_verdict(self, candidate_key, context_fp, bible_fp, item,
                       sentence_numbers=()):
        """Store a fully-built scored item under the current
        ``VERDICT_CODE_VERSION``.

        First evaluation wins: a repeat record for the same key
        updates ``last_seen``/``hits`` and keeps the FIRST item.
        A non-dict item, or one that cannot be JSON-serialised,
        stores nothing and returns False — an unserialisable
        verdict is not evidence and must not poison the store."""
        if _blank(candidate_key):
            return False
        if not isinstance(item, dict):
            return False
        try:
            item_json = json.dumps(item)
        except (TypeError, ValueError):
            return False
        try:
            numbers_json = json.dumps(list(sentence_numbers or ()))
        except (TypeError, ValueError):
            numbers_json = "[]"
        accepted = item.get("accepted")
        rejection_reason = item.get("rejection_reason") or item.get("reason")
        rejection_stage = item.get("rejection_stage") or item.get("stage")
        try:
            with self._lock:
                conn = self._write_conn()
                if conn is None:
                    return False
                now = self._now()
                conn.execute(
                    "INSERT INTO verdicts"
                    "(candidate_key, context_fp, bible_fp, code_version,"
                    " item_json, accepted, rejection_reason,"
                    " rejection_stage, sentence_numbers, first_seen,"
                    " last_seen, hits)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,1)"
                    " ON CONFLICT(candidate_key, context_fp, bible_fp,"
                    " code_version) DO UPDATE SET"
                    " last_seen=excluded.last_seen,"
                    " hits=verdicts.hits+1",
                    (candidate_key, context_fp, bible_fp,
                     VERDICT_CODE_VERSION, item_json,
                     1 if accepted else 0, rejection_reason,
                     rejection_stage, numbers_json, now, now),
                )
                conn.commit()
                return True
        except Exception:
            return False

    def verdict(self, candidate_key, context_fp, bible_fp):
        """A deep copy of the stored item for exactly this
        (candidate, context, bible, current code version), or None.

        A bible or code-version mismatch is a miss by construction
        (see the module docstring's expiry rules). A hit bumps
        ``hits``/``last_seen``. The returned dict is freshly
        deserialised, so mutating it cannot corrupt the store."""
        if _blank(candidate_key):
            return None
        try:
            with self._lock:
                conn = self._read_conn()
                if conn is None:
                    return None
                row = conn.execute(
                    "SELECT item_json FROM verdicts"
                    " WHERE candidate_key=? AND context_fp=?"
                    " AND bible_fp=? AND code_version=?",
                    (candidate_key, context_fp, bible_fp,
                     VERDICT_CODE_VERSION),
                ).fetchone()
                if row is None:
                    return None
                try:
                    item = json.loads(row["item_json"])
                except (TypeError, ValueError):
                    return None
                if not isinstance(item, dict):
                    return None
                try:
                    write = self._write_conn()
                    if write is not None:
                        write.execute(
                            "UPDATE verdicts SET hits=hits+1, last_seen=?"
                            " WHERE candidate_key=? AND context_fp=?"
                            " AND bible_fp=? AND code_version=?",
                            (self._now(), candidate_key, context_fp,
                             bible_fp, VERDICT_CODE_VERSION),
                        )
                        write.commit()
                except Exception:
                    pass
                return copy.deepcopy(item)
        except Exception:
            return None

    def record_intrinsic_verdict(self, candidate_key, item):
        """Record a context-independent (intrinsic) verdict.

        Call ONLY for rejections that are properties of the
        candidate itself (banned provider, foreign provenance,
        social platform, meme format). These never expire and are
        bible-independent. First record wins, as for verdicts."""
        if _blank(candidate_key):
            return False
        if not isinstance(item, dict):
            return False
        try:
            item_json = json.dumps(item)
        except (TypeError, ValueError):
            return False
        rejection_reason = item.get("rejection_reason") or item.get("reason")
        try:
            with self._lock:
                conn = self._write_conn()
                if conn is None:
                    return False
                now = self._now()
                conn.execute(
                    "INSERT INTO intrinsic_verdicts"
                    "(candidate_key, item_json, rejection_reason,"
                    " first_seen, last_seen, hits)"
                    " VALUES (?,?,?,?,?,1)"
                    " ON CONFLICT(candidate_key) DO UPDATE SET"
                    " last_seen=excluded.last_seen,"
                    " hits=intrinsic_verdicts.hits+1",
                    (candidate_key, item_json, rejection_reason, now, now),
                )
                conn.commit()
                return True
        except Exception:
            return False

    def intrinsic_verdict(self, candidate_key):
        """A deep copy of the intrinsic verdict item, or None.
        A hit bumps ``hits``/``last_seen``."""
        if _blank(candidate_key):
            return None
        try:
            with self._lock:
                conn = self._read_conn()
                if conn is None:
                    return None
                row = conn.execute(
                    "SELECT item_json FROM intrinsic_verdicts"
                    " WHERE candidate_key=?",
                    (candidate_key,),
                ).fetchone()
                if row is None:
                    return None
                try:
                    item = json.loads(row["item_json"])
                except (TypeError, ValueError):
                    return None
                if not isinstance(item, dict):
                    return None
                try:
                    write = self._write_conn()
                    if write is not None:
                        write.execute(
                            "UPDATE intrinsic_verdicts"
                            " SET hits=hits+1, last_seen=?"
                            " WHERE candidate_key=?",
                            (self._now(), candidate_key),
                        )
                        write.commit()
                except Exception:
                    pass
                return copy.deepcopy(item)
        except Exception:
            return None

    # ------------------------------------------------------------ demotions

    def record_demotion(self, candidate_key, segment_key, context_fp, *,
                        stage, reason):
        """Record a post-download rejection (frame verification,
        transcript mismatch, speculative-unconfirmed, ...).
        Demotions never expire by time. Returns True when recorded."""
        if _blank(candidate_key):
            return False
        try:
            with self._lock:
                conn = self._write_conn()
                if conn is None:
                    return False
                now = self._now()
                conn.execute(
                    "INSERT INTO demotions"
                    "(candidate_key, segment_key, context_fp, stage,"
                    " reason, first_seen, last_seen, hits)"
                    " VALUES (?,?,?,?,?,?,?,1)"
                    " ON CONFLICT(candidate_key, segment_key, context_fp)"
                    " DO UPDATE SET stage=excluded.stage,"
                    " reason=excluded.reason,"
                    " last_seen=excluded.last_seen,"
                    " hits=demotions.hits+1",
                    (candidate_key, segment_key, context_fp, stage,
                     reason, now, now),
                )
                conn.commit()
                return True
        except Exception:
            return False

    def demotion(self, candidate_key, segment_key, context_fp):
        """The demotion for exactly this (candidate, segment,
        context), or None. A context or segment mismatch is a
        miss. A hit bumps ``hits``/``last_seen``."""
        if _blank(candidate_key):
            return None
        try:
            with self._lock:
                conn = self._read_conn()
                if conn is None:
                    return None
                row = conn.execute(
                    "SELECT * FROM demotions WHERE candidate_key=?"
                    " AND segment_key=? AND context_fp=?",
                    (candidate_key, segment_key, context_fp),
                ).fetchone()
                if row is None:
                    return None
                result = {
                    "candidate_key": row["candidate_key"],
                    "segment_key": row["segment_key"],
                    "context_fp": row["context_fp"],
                    "stage": row["stage"],
                    "reason": row["reason"],
                    "first_seen": row["first_seen"],
                    "last_seen": row["last_seen"],
                    "hits": int(row["hits"] or 0),
                }
                try:
                    write = self._write_conn()
                    if write is not None:
                        write.execute(
                            "UPDATE demotions SET hits=hits+1, last_seen=?"
                            " WHERE candidate_key=? AND segment_key=?"
                            " AND context_fp=?",
                            (self._now(), candidate_key, segment_key,
                             context_fp),
                        )
                        write.commit()
                        result["hits"] += 1
                except Exception:
                    pass
                return result
        except Exception:
            return None

    # ----------------------------------------------------- sentence states

    def note_sentence_state(self, sentence_number, state, detail=None):
        """Record a sentence's discovery state; latest write wins.
        Returns True when recorded."""
        if sentence_number is None:
            return False
        try:
            number = int(sentence_number)
        except (TypeError, ValueError):
            return False
        try:
            with self._lock:
                conn = self._write_conn()
                if conn is None:
                    return False
                conn.execute(
                    "INSERT INTO sentence_states"
                    "(sentence_number, state, detail, updated)"
                    " VALUES (?,?,?,?)"
                    " ON CONFLICT(sentence_number) DO UPDATE SET"
                    " state=excluded.state, detail=excluded.detail,"
                    " updated=excluded.updated",
                    (number, state, detail, self._now()),
                )
                conn.commit()
                return True
        except Exception:
            return False

    def sentence_state(self, sentence_number):
        """One sentence's discovery state, or None."""
        if sentence_number is None:
            return None
        try:
            number = int(sentence_number)
        except (TypeError, ValueError):
            return None
        try:
            with self._lock:
                conn = self._read_conn()
                if conn is None:
                    return None
                row = conn.execute(
                    "SELECT * FROM sentence_states WHERE sentence_number=?",
                    (number,),
                ).fetchone()
                if row is None:
                    return None
                return {
                    "sentence_number": int(row["sentence_number"]),
                    "state": row["state"],
                    "detail": row["detail"],
                    "updated": row["updated"],
                }
        except Exception:
            return None

    def sentence_states(self):
        """All recorded sentence states, keyed by sentence number."""
        try:
            with self._lock:
                conn = self._read_conn()
                if conn is None:
                    return {}
                rows = conn.execute(
                    "SELECT * FROM sentence_states ORDER BY sentence_number"
                ).fetchall()
                return {
                    int(row["sentence_number"]): {
                        "sentence_number": int(row["sentence_number"]),
                        "state": row["state"],
                        "detail": row["detail"],
                        "updated": row["updated"],
                    }
                    for row in rows
                }
        except Exception:
            return {}

    # ------------------------------------------------- evidence ledger
    #
    # Phase 3: the per-production evidence ledger — one row per
    # (candidate, sentence) the verifier accepted, upgraded at the
    # fetch commit point with the downloaded bytes' content hash
    # and the accepted outcome. It is the authoritative record the
    # global assignment pass, the duplicate-content guard and the
    # asset report read: what evidence was secured, for which
    # sentence, from which source, at which tier and with which
    # authenticity classification. Rows are keyed
    # (candidate_id, sentence_number); a re-record never weakens a
    # row — outcome strength is accepted > verified, and a known
    # content hash / duplicate link is never erased by a later
    # hash-less record.

    _EVIDENCE_OUTCOME_RANK = {"verified": 1, "accepted": 2}

    def record_evidence(self, entry):
        """Insert or strengthen one evidence row. Returns True when
        recorded. Never raises (the store's fail-soft contract)."""
        if not isinstance(entry, dict):
            return False
        candidate_id = entry.get("candidate_id")
        sentence_number = entry.get("sentence_number")
        if _blank(candidate_id) or sentence_number is None:
            return False
        try:
            sentence_number = int(sentence_number)
        except (TypeError, ValueError):
            return False
        outcome = str(entry.get("outcome") or "verified")
        try:
            with self._lock:
                conn = self._write_conn()
                if conn is None:
                    return False
                row = conn.execute(
                    "SELECT outcome, content_hash, duplicate_of"
                    " FROM evidence"
                    " WHERE candidate_id=? AND sentence_number=?",
                    (str(candidate_id), sentence_number),
                ).fetchone()
                if row is not None:
                    old_rank = self._EVIDENCE_OUTCOME_RANK.get(
                        row["outcome"], 0
                    )
                    new_rank = self._EVIDENCE_OUTCOME_RANK.get(outcome, 0)
                    if new_rank < old_rank:
                        outcome = row["outcome"]
                conn.execute(
                    "INSERT INTO evidence"
                    "(candidate_id, sentence_number, scene_number,"
                    " beat_id, url, provider, kind, tier,"
                    " classification, score, outcome, content_hash,"
                    " segment_id, duplicate_of, recorded_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(candidate_id, sentence_number)"
                    " DO UPDATE SET"
                    " scene_number=COALESCE(excluded.scene_number,"
                    " evidence.scene_number),"
                    " beat_id=COALESCE(excluded.beat_id,"
                    " evidence.beat_id),"
                    " url=excluded.url,"
                    " provider=excluded.provider,"
                    " kind=excluded.kind,"
                    " tier=excluded.tier,"
                    " classification=excluded.classification,"
                    " score=excluded.score,"
                    " outcome=excluded.outcome,"
                    " content_hash=COALESCE(excluded.content_hash,"
                    " evidence.content_hash),"
                    " segment_id=COALESCE(excluded.segment_id,"
                    " evidence.segment_id),"
                    " duplicate_of=COALESCE(excluded.duplicate_of,"
                    " evidence.duplicate_of),"
                    " recorded_at=excluded.recorded_at",
                    (
                        str(candidate_id),
                        sentence_number,
                        entry.get("scene_number"),
                        entry.get("beat_id"),
                        str(entry.get("url") or ""),
                        str(entry.get("provider") or "unknown"),
                        str(entry.get("kind") or ""),
                        str(entry.get("tier") or "direct"),
                        str(entry.get("classification") or "unknown"),
                        entry.get("score"),
                        outcome,
                        entry.get("content_hash"),
                        entry.get("segment_id"),
                        entry.get("duplicate_of"),
                        self._now(),
                    ),
                )
                conn.commit()
                return True
        except Exception:
            return False

    def mark_evidence_duplicate(self, candidate_id, sentence_number,
                                *, content_hash=None, duplicate_of=None):
        """Link an existing evidence row to the bytes it turned out
        to duplicate: sets ``content_hash`` and ``duplicate_of``
        without touching the row's verification fields. Used by the
        download dispatcher's cross-beat duplicate guard (in-run and
        durable) so the ledger itself records that these bytes are
        the owning sentence's evidence, not a second piece of it.
        Returns True when a row was updated."""
        if _blank(candidate_id) or sentence_number is None:
            return False
        try:
            number = int(sentence_number)
        except (TypeError, ValueError):
            return False
        try:
            with self._lock:
                conn = self._write_conn()
                if conn is None:
                    return False
                cursor = conn.execute(
                    "UPDATE evidence SET"
                    " content_hash=COALESCE(?, content_hash),"
                    " duplicate_of=COALESCE(?, duplicate_of)"
                    " WHERE candidate_id=? AND sentence_number=?",
                    (
                        content_hash,
                        duplicate_of,
                        str(candidate_id),
                        number,
                    ),
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception:
            return False

    def _evidence_row(self, row):
        return {
            "candidate_id": row["candidate_id"],
            "sentence_number": row["sentence_number"],
            "scene_number": row["scene_number"],
            "beat_id": row["beat_id"],
            "url": row["url"],
            "provider": row["provider"],
            "kind": row["kind"],
            "tier": row["tier"],
            "classification": row["classification"],
            "score": row["score"],
            "outcome": row["outcome"],
            "content_hash": row["content_hash"],
            "segment_id": row["segment_id"],
            "duplicate_of": row["duplicate_of"],
            "recorded_at": row["recorded_at"],
        }

    def evidence_for_sentence(self, sentence_number):
        """All evidence rows for one sentence, strongest first."""
        if sentence_number is None:
            return []
        try:
            number = int(sentence_number)
        except (TypeError, ValueError):
            return []
        try:
            with self._lock:
                conn = self._read_conn()
                if conn is None:
                    return []
                rows = conn.execute(
                    "SELECT * FROM evidence WHERE sentence_number=?"
                    " ORDER BY score DESC",
                    (number,),
                ).fetchall()
                return [self._evidence_row(row) for row in rows]
        except Exception:
            return []

    def evidence_entries(self):
        """Every evidence row in the ledger, sentence order."""
        try:
            with self._lock:
                conn = self._read_conn()
                if conn is None:
                    return []
                rows = conn.execute(
                    "SELECT * FROM evidence"
                    " ORDER BY sentence_number, score DESC"
                ).fetchall()
                return [self._evidence_row(row) for row in rows]
        except Exception:
            return []

    def evidence_content_owner(self, content_hash):
        """The accepted evidence row owning *content_hash*, or
        None. Only ACCEPTED rows own content: a verified-but-never-
        downloaded candidate's hash is unknown by construction, and
        a duplicate row (``duplicate_of`` set) never owns — ownership
        belongs to the first accepted row with those bytes, which is
        what makes byte-identical photographs one piece of evidence,
        not two (acceptance #11)."""
        if _blank(content_hash):
            return None
        try:
            with self._lock:
                conn = self._read_conn()
                if conn is None:
                    return None
                row = conn.execute(
                    "SELECT * FROM evidence"
                    " WHERE content_hash=? AND outcome='accepted'"
                    " AND duplicate_of IS NULL"
                    " ORDER BY recorded_at ASC LIMIT 1",
                    (str(content_hash),),
                ).fetchone()
                if row is None:
                    return None
                return self._evidence_row(row)
        except Exception:
            return None

    # -------------------------------------------------------------- summary

    def summary(self):
        """Row counts per table, for reports and tests. Never raises;
        an unusable store reports zero counts."""
        counts = {table: 0 for table in _TABLES}
        try:
            with self._lock:
                conn = self._read_conn()
                if conn is None:
                    return counts
                for table in _TABLES:
                    try:
                        row = conn.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()
                        counts[table] = int(row[0]) if row else 0
                    except Exception:
                        counts[table] = 0
                return counts
        except Exception:
            return counts


def close_all():
    """Module-level convenience: close every cached store."""
    DiscoveryMemory.close_all()
