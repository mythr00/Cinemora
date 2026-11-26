"""Phase 2 (Discovery Efficiency): durable discovery memory.

Pins the contract of ``services.discovery_memory`` — the per-
production store that lets discovery stop re-downloading and
re-verifying candidates already known dead across scenes, runs
and process restarts:

* round-trips for download failures/successes, verdicts,
  intrinsic verdicts, demotions and sentence states;
* the expiry rules stated in the module docstring (transient
  download failures expire after 24h, deterministic ones never;
  verdicts are keyed by bible fingerprint + code version and so
  miss by construction when either changes);
* first-evaluation-wins for verdict items;
* the fail-soft contract: no method ever raises, blank keys are
  no-ops, an unusable project path or a database file deleted
  mid-flight degrades to honest misses.
"""

import sqlite3

import pytest

from services import discovery_memory
from services.discovery_memory import DiscoveryMemory


class FakeClock:
    def __init__(self, start=1_700_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def _close_stores():
    yield
    discovery_memory.close_all()


@pytest.fixture()
def store(tmp_path):
    instance = DiscoveryMemory.open(tmp_path)
    assert instance is not None
    return instance


def _hits(db_path, table, where, params):
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            f"SELECT hits FROM {table} WHERE {where}", params
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# --------------------------------------------------------------- lifecycle

def test_open_creates_file_and_is_cached(tmp_path):
    first = DiscoveryMemory.open(tmp_path)
    assert first is not None
    assert (tmp_path / "discovery_memory.sqlite3").is_file()
    second = DiscoveryMemory.open(tmp_path)
    assert second is first


def test_close_all_resets_cache(tmp_path):
    first = DiscoveryMemory.open(tmp_path)
    discovery_memory.close_all()
    second = DiscoveryMemory.open(tmp_path)
    assert second is not None
    assert second is not first


def test_open_missing_dir_returns_none(tmp_path):
    assert DiscoveryMemory.open(tmp_path / "does-not-exist") is None


# --------------------------------------------------------- download failures

def test_download_failure_round_trip_per_scope(store):
    for scope, key in (
        ("source", "yt:abc"),
        ("segment", "seg:42"),
        ("download_url", "https://cdn.example/v.mp4"),
    ):
        assert store.record_download_failure(
            scope, key, failure_class="http_404", retryable=False,
            reason="not found",
        )
        got = store.download_failure(scope, key)
        assert got is not None
        assert got["failure_class"] == "http_404"
        assert got["retryable"] is False
        assert got["reason"] == "not found"
        assert got["attempts"] == 1
        assert got["first_seen"] == got["last_seen"]
    # Scopes are independent namespaces for the same key string.
    assert store.download_failure("source", "seg:42") is None
    assert store.download_failure("source", "never-seen") is None


def test_download_failure_attempts_increment_and_latest_wins(store):
    store.record_download_failure(
        "source", "k1", failure_class="timeout", retryable=True,
        reason="slow",
    )
    store.record_download_failure(
        "source", "k1", failure_class="http_410", retryable=False,
        reason="gone",
    )
    got = store.download_failure("source", "k1")
    assert got["attempts"] == 2
    assert got["failure_class"] == "http_410"
    assert got["retryable"] is False
    assert got["reason"] == "gone"


def test_transient_failure_expires_after_ttl(tmp_path):
    clock = FakeClock()
    instance = DiscoveryMemory.open(tmp_path, clock=clock)
    instance.record_download_failure(
        "download_url", "u1", failure_class="timeout", retryable=True,
        reason="timed out",
    )
    clock.advance(23 * 3600)
    assert instance.download_failure("download_url", "u1") is not None
    clock.advance(2 * 3600)  # now +25h since last_seen
    assert instance.download_failure("download_url", "u1") is None
    # Lazily deleted: a second lookup stays a miss.
    assert instance.download_failure("download_url", "u1") is None


def test_deterministic_failure_never_expires(tmp_path):
    clock = FakeClock()
    instance = DiscoveryMemory.open(tmp_path, clock=clock)
    instance.record_download_failure(
        "download_url", "u1", failure_class="http_404", retryable=False,
        reason="not found",
    )
    clock.advance(30 * 24 * 3600)
    got = instance.download_failure("download_url", "u1")
    assert got is not None
    assert got["failure_class"] == "http_404"


def test_download_success_supersedes_failure_and_round_trips(store):
    store.record_download_failure(
        "download_url", "u1", failure_class="timeout", retryable=True,
        reason="timed out",
    )
    # A failure under a different scope for the same key survives:
    # only the download_url row is superseded.
    store.record_download_failure(
        "source", "u1", failure_class="http_404", retryable=False,
        reason="dead source",
    )
    assert store.record_download_success(
        "u1", content_hash="sha256:abc", sentence_number=7
    )
    assert store.download_failure("download_url", "u1") is None
    assert store.download_failure("source", "u1") is not None
    got = store.download_success("u1")
    assert got == {
        "content_hash": "sha256:abc",
        "sentence_number": 7,
        "seen": got["seen"],
    }
    assert store.download_success("never-downloaded") is None


# ---------------------------------------------------------------- verdicts

def test_verdict_round_trip_returns_a_copy(store, tmp_path):
    item = {"accepted": False, "rejection_reason": "wrong_person",
            "rejection_stage": "frame_verification", "score": 0.12,
            "nested": {"tags": ["a"]}}
    assert store.record_verdict(
        "cand:1", "ctx:1", "bible:1", item, sentence_numbers=(3, 4)
    )
    got = store.verdict("cand:1", "ctx:1", "bible:1")
    assert got == item
    # Mutating the returned dict (including nested values) must
    # not corrupt the store.
    got["score"] = 9.99
    got["nested"]["tags"].append("corrupt")
    assert store.verdict("cand:1", "ctx:1", "bible:1") == item


def test_verdict_miss_on_bible_and_context_mismatch(store):
    item = {"accepted": True, "score": 0.9}
    store.record_verdict("cand:1", "ctx:1", "bible:1", item)
    assert store.verdict("cand:1", "ctx:1", "bible:2") is None
    assert store.verdict("cand:1", "ctx:2", "bible:1") is None
    assert store.verdict("cand:2", "ctx:1", "bible:1") is None


def test_verdict_miss_after_code_version_change(store, monkeypatch):
    item = {"accepted": True, "score": 0.9}
    store.record_verdict("cand:1", "ctx:1", "bible:1", item)
    assert store.verdict("cand:1", "ctx:1", "bible:1") == item
    monkeypatch.setattr(
        discovery_memory, "VERDICT_CODE_VERSION", "2"
    )
    assert store.verdict("cand:1", "ctx:1", "bible:1") is None


def test_verdict_first_record_wins_and_hits_increment(store, tmp_path):
    first = {"accepted": False, "rejection_reason": "first"}
    second = {"accepted": True, "rejection_reason": "second"}
    store.record_verdict("cand:1", "ctx:1", "bible:1", first)
    store.record_verdict("cand:1", "ctx:1", "bible:1", second)
    assert store.verdict("cand:1", "ctx:1", "bible:1") == first
    db_path = tmp_path / "discovery_memory.sqlite3"
    hits_after_record_and_read = _hits(
        db_path, "verdicts",
        "candidate_key=? AND context_fp=? AND bible_fp=?",
        ("cand:1", "ctx:1", "bible:1"),
    )
    # 1 (first record) + 1 (repeat record) + 1 (the read above).
    assert hits_after_record_and_read == 3
    store.verdict("cand:1", "ctx:1", "bible:1")
    assert _hits(
        db_path, "verdicts",
        "candidate_key=? AND context_fp=? AND bible_fp=?",
        ("cand:1", "ctx:1", "bible:1"),
    ) == 4


def test_verdict_non_dict_and_unserialisable_items_rejected(store):
    assert store.record_verdict("c", "x", "b", "not-a-dict") is False
    assert store.record_verdict("c", "x", "b", None) is False
    assert store.record_verdict(
        "c", "x", "b", {"bad": object()}
    ) is False
    assert store.verdict("c", "x", "b") is None


def test_intrinsic_verdict_round_trip_bible_independent(store):
    item = {"accepted": False,
            "rejection_reason": "social_platform"}
    assert store.record_intrinsic_verdict("cand:9", item)
    got = store.intrinsic_verdict("cand:9")
    assert got == item
    # No bible/context is involved at all: the lookup takes only
    # the candidate key, and mutating the copy is harmless.
    got["rejection_reason"] = "tampered"
    assert store.intrinsic_verdict("cand:9") == item
    assert store.intrinsic_verdict("cand:unknown") is None


# --------------------------------------------------------------- demotions

def test_demotion_round_trip_and_key_misses(store):
    assert store.record_demotion(
        "cand:1", "seg:1", "ctx:1",
        stage="frame_verification", reason="wrong person in frame",
    )
    got = store.demotion("cand:1", "seg:1", "ctx:1")
    assert got is not None
    assert got["stage"] == "frame_verification"
    assert got["reason"] == "wrong person in frame"
    assert store.demotion("cand:1", "seg:1", "ctx:OTHER") is None
    assert store.demotion("cand:1", "seg:OTHER", "ctx:1") is None
    assert store.demotion("cand:OTHER", "seg:1", "ctx:1") is None


# ----------------------------------------------------------- sentence states

def test_sentence_states_latest_wins_and_map(store):
    assert store.note_sentence_state(3, "searched", detail="2 queries")
    assert store.note_sentence_state(5, "covered")
    got = store.sentence_state(3)
    assert got["state"] == "searched"
    assert got["detail"] == "2 queries"
    assert got["sentence_number"] == 3

    assert store.note_sentence_state(3, "covered", detail="asset 9")
    assert store.sentence_state(3)["state"] == "covered"
    assert store.sentence_state(3)["detail"] == "asset 9"

    states = store.sentence_states()
    assert set(states) == {3, 5}
    assert states[5]["state"] == "covered"
    assert states[5]["detail"] is None
    assert store.sentence_state(99) is None


def test_summary_counts(store):
    store.record_download_failure("source", "k", retryable=False)
    store.record_download_success("u")
    store.record_verdict("c", "x", "b", {"accepted": True})
    store.record_intrinsic_verdict("c", {"accepted": False})
    store.record_demotion("c", "s", "x", stage="st", reason="r")
    store.note_sentence_state(1, "searched")
    summary = store.summary()
    assert summary == {
        "download_failures": 1,
        "download_successes": 1,
        "verdicts": 1,
        "intrinsic_verdicts": 1,
        "demotions": 1,
        "sentence_states": 1,
    }


# ---------------------------------------------------------------- fail-soft

def test_open_on_a_file_path_never_raises(tmp_path):
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("plain file", encoding="utf-8")
    # Pinned behaviour of this implementation: open() returns None.
    assert DiscoveryMemory.open(file_path) is None
    # Direct construction yields an inert instance whose methods
    # no-op without raising.
    inert = DiscoveryMemory(file_path)
    assert inert.record_download_failure("s", "k") is False
    assert inert.download_failure("s", "k") is None
    assert inert.record_verdict("c", "x", "b", {"a": 1}) is False
    assert inert.verdict("c", "x", "b") is None
    assert inert.sentence_states() == {}
    assert inert.summary()["verdicts"] == 0
    inert.close()


def test_deleted_db_file_mid_flight_never_raises(store, tmp_path):
    store.record_download_failure(
        "source", "k", failure_class="http_404", retryable=False,
        reason="gone",
    )
    assert store.download_failure("source", "k") is not None
    db_path = tmp_path / "discovery_memory.sqlite3"
    # Remove the database (and any WAL sidecars) under the open store.
    discovery_memory.close_all()
    for suffix in ("", "-wal", "-shm"):
        try:
            db_path.with_name(db_path.name + suffix).unlink()
        except FileNotFoundError:
            pass
    assert not db_path.exists()
    # The (now closed) instance answers honest misses, never raises.
    assert store.download_failure("source", "k") is None
    assert store.verdict("c", "x", "b") is None
    assert store.summary()["download_failures"] == 0


def test_deleted_db_file_open_instance_answers_none(tmp_path):
    instance = DiscoveryMemory(tmp_path)
    instance.record_download_failure(
        "source", "k", failure_class="http_404", retryable=False,
        reason="gone",
    )
    db_path = tmp_path / "discovery_memory.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        try:
            db_path.with_name(db_path.name + suffix).unlink()
        except FileNotFoundError:
            pass
    # Still-open instance: lookups are misses, not exceptions.
    assert instance.download_failure("source", "k") is None
    assert instance.download_success("u") is None
    assert instance.sentence_state(1) is None
    instance.close()


def test_blank_keys_are_noops(store):
    assert store.record_download_failure("", "k") is False
    assert store.record_download_failure("source", "") is False
    assert store.record_download_failure("source", "   ") is False
    assert store.record_download_failure(None, "k") is False
    assert store.download_failure("", "k") is None
    assert store.download_failure("source", "") is None
    assert store.record_download_success("") is False
    assert store.download_success("") is None
    assert store.record_verdict("", "x", "b", {"a": 1}) is False
    assert store.verdict("", "x", "b") is None
    assert store.record_intrinsic_verdict("", {"a": 1}) is False
    assert store.intrinsic_verdict("") is None
    assert store.record_demotion(
        "", "s", "x", stage="st", reason="r"
    ) is False
    assert store.demotion("", "s", "x") is None
    assert store.summary() == {
        "download_failures": 0,
        "download_successes": 0,
        "verdicts": 0,
        "intrinsic_verdicts": 0,
        "demotions": 0,
        "sentence_states": 0,
    }


def test_schema_version_mismatch_treated_as_empty(tmp_path):
    instance = DiscoveryMemory.open(tmp_path)
    instance.record_download_failure(
        "source", "k", failure_class="http_404", retryable=False,
        reason="gone",
    )
    assert instance.download_failure("source", "k") is not None
    discovery_memory.close_all()
    # Corrupt the recorded schema version, then reopen: the store
    # is recreated empty rather than raising.
    db_path = tmp_path / "discovery_memory.sqlite3"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE meta SET value='999' WHERE key='schema_version'"
        )
        conn.commit()
    finally:
        conn.close()
    reopened = DiscoveryMemory.open(tmp_path)
    assert reopened is not None
    assert reopened.download_failure("source", "k") is None
    assert reopened.summary()["download_failures"] == 0
