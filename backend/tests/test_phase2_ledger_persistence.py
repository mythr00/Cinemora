"""Master Directive Phase 2 (discovery efficiency) — persistence
of the tier-ladder EscalationLedger across Phase 1 same-run
resume.

Phase 1 persists VisualRegistry.export_state() into scene
checkpoints and stage payloads and restores it on resume, but the
escalation ledger was not part of that state: a resumed run
restarted escalation from zero and could double-spend the D-B2
production budgets (40 extra searches / 20 extra downloads).
Pinned here:

- ledger export -> JSON round-trip -> restore reproduces the
  original ledger's summary() exactly;
- double-spend protection: account totals survive the restore
  boundary, so only the remainder of the production cap can be
  allocated afterwards (and nothing once the cap is reached);
- attempt idempotency survives: a recorded verified batch is
  recognised as a repeat after restore, with its pool replayed;
- per-need fairness state and per-beat speculative counts survive;
- registry wiring: export_state carries "escalation_ledger" only
  when a ledger already exists, restore_state reattaches it where
  tier_ladder.ledger_for finds it, re-anchored to the restored
  registry's own clock;
- malformed restore data never raises and never invents spends.
"""

import json

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import tier_ladder
from services.visual_registry import VisualRegistry

NEED_A = ("beat-1", 0, 3)
NEED_B = ("beat-2", 1, 7)
NEED_C = ("beat-3", 2, 11)
BEAT_KEY = ("/scenes/scene_04", 4)
QUERIES = ["Verdict Forms 2021", "court verdict"]
POOL = [
    {"url": "https://example.test/a", "title": "Verdict forms", "score": 0.9},
    {"url": "https://example.test/b", "title": "Courtroom", "score": 0.7},
]


def _driven_ledger():
    """A ledger with representative activity driven entirely
    through the public API: registered needs (one unspent),
    breadth + deepening searches, downloads, a speculative
    download, and a recorded verified attempt that has already
    been recognised once as a repeat."""
    ledger = tier_ladder.EscalationLedger()
    ledger.register_need(NEED_A)
    ledger.register_need(NEED_B)
    ledger.register_need(NEED_C)
    ledger.spend_searches(2, need_key=NEED_A)  # breadth + deepening
    ledger.spend_searches(1, need_key=NEED_B)  # breadth
    ledger.spend_download(need_key=NEED_A)
    ledger.spend_download(need_key=NEED_A)  # deepening download
    assert ledger.can_spend_speculative_download(beat_key=BEAT_KEY)
    ledger.spend_speculative_download(beat_key=BEAT_KEY)
    ledger.record_attempt(
        "related", QUERIES,
        outcome="verified", verified=2, pool_items=POOL,
        need_key=NEED_A,
    )
    assert ledger.completed_attempt("related", QUERIES, need_key=NEED_A)
    return ledger


def _round_trip(ledger):
    """Export -> strict JSON round-trip -> restore into a fresh
    ledger, exactly as the Phase 1 checkpoint writer does."""
    payload = json.loads(json.dumps(ledger.export_state()))
    return tier_ladder.EscalationLedger().restore_state(payload)


def test_export_json_round_trip_restore_summary_matches():
    ledger = _driven_ledger()
    restored = _round_trip(ledger)
    assert restored.summary() == ledger.summary()


def test_export_open_escalation_interval_is_included_not_mutated():
    now = [1000.0]
    ledger = tier_ladder.EscalationLedger(clock=lambda: now[0])
    ledger.begin_escalation()
    now[0] += 5.0
    ledger.end_escalation()
    ledger.begin_escalation()
    now[0] += 3.0
    payload = json.loads(json.dumps(ledger.export_state()))
    # The open interval is closed into the exported total...
    assert payload["escalation_seconds"] == 8.0
    # ...without mutating the live ledger's open interval.
    assert ledger.escalation_seconds == 5.0
    assert ledger._active_started is not None
    restored = tier_ladder.EscalationLedger().restore_state(payload)
    assert restored.escalation_seconds == 8.0
    assert restored._active_started is None


def test_double_spend_protection_across_restore():
    ledger = tier_ladder.EscalationLedger()
    ledger.spend_searches(35)
    restored = _round_trip(ledger)
    assert restored.searches == 35
    # Only the remainder of the production cap may still execute.
    assert restored.allocate_searches(10) == 5
    restored.spend_searches(5)
    assert restored.searches == tier_ladder.PRODUCTION_MAX_EXTRA_SEARCHES
    assert restored.allocate_searches(1) == 0
    assert restored.allocate_searches(1, need_key=NEED_A) == 0


def test_attempt_idempotency_survives_restore():
    restored = _round_trip(_driven_ledger())
    repeats_before = restored.attempt_repeats
    record = restored.completed_attempt("related", QUERIES, need_key=NEED_A)
    assert record is not None
    assert record["outcome"] == "verified"
    assert record["pool_items"] == POOL
    assert record["need_key"] == NEED_A
    assert restored.attempt_repeats == repeats_before + 1
    # A batch never recorded still misses after restore.
    assert restored.completed_attempt(
        "related", ["some other query"], need_key=NEED_A
    ) is None


def test_per_need_fairness_state_survives_restore():
    ledger = _driven_ledger()
    restored = _round_trip(ledger)
    assert restored.needs_with_first_attempt() == 2
    assert restored.needs_registered() == ledger.needs_registered()
    # The registered-but-unspent need still holds its reserve slot.
    assert restored.fairness_reserve() == ledger.fairness_reserve()
    assert restored.summary()["fairness_reserve_searches"] == (
        ledger.summary()["fairness_reserve_searches"]
    )


def test_speculative_per_beat_counts_survive_restore():
    ledger = tier_ladder.EscalationLedger()
    cap = tier_ladder.SPECULATIVE_PER_BEAT_DOWNLOAD_CAP
    for _ in range(cap):
        assert ledger.can_spend_speculative_download(beat_key=BEAT_KEY)
        ledger.spend_speculative_download(beat_key=BEAT_KEY)
    assert not ledger.can_spend_speculative_download(beat_key=BEAT_KEY)
    restored = _round_trip(ledger)
    assert restored.speculative_downloads == cap
    # The per-beat cap still refuses across the restore boundary...
    assert not restored.can_spend_speculative_download(beat_key=BEAT_KEY)
    # ...while a different beat may still spend.
    assert restored.can_spend_speculative_download(
        beat_key=("/scenes/scene_05", 1)
    )


def test_non_serialisable_pool_items_are_dropped_not_fatal():
    ledger = tier_ladder.EscalationLedger()
    ledger.record_attempt(
        "related", QUERIES,
        outcome="verified", verified=2,
        pool_items=[POOL[0], {"url": "https://x.test", "blob": object()}],
        need_key=NEED_A,
    )
    payload = json.loads(json.dumps(ledger.export_state()))
    restored = tier_ladder.EscalationLedger().restore_state(payload)
    record = restored.completed_attempt("related", QUERIES, need_key=NEED_A)
    assert record is not None
    assert record["pool_items"] == [POOL[0]]


def test_registry_wiring_round_trip():
    registry = VisualRegistry(project_id="p1")
    ledger = tier_ladder.ledger_for(registry)
    ledger.register_need(NEED_A)
    ledger.spend_searches(3, need_key=NEED_A)
    ledger.spend_download(need_key=NEED_A)
    state = registry.export_state()
    assert "escalation_ledger" in state
    payload = json.loads(json.dumps(state))  # strict serialisability
    restored_registry = VisualRegistry(project_id="p1").restore_state(payload)
    restored_ledger = tier_ladder.ledger_for(restored_registry)
    # ledger_for returns the RESTORED ledger, not a fresh one.
    assert restored_ledger is restored_registry._tier_escalation_ledger
    assert restored_ledger.searches == 3
    assert restored_ledger.downloads == 1
    assert restored_ledger.needs_with_first_attempt() == 1
    # Re-anchored to the restored registry's own clock.
    assert restored_ledger.asset_stage_started_at == restored_registry.created_at


def test_registry_without_ledger_exports_no_key():
    registry = VisualRegistry(project_id="p1")
    state = registry.export_state()
    assert "escalation_ledger" not in state
    json.dumps(state)
    # Exporting created nothing: ledger_for still makes a fresh,
    # zeroed ledger afterwards.
    ledger = tier_ladder.ledger_for(registry)
    assert ledger.searches == 0
    assert ledger.summary()["attempts_recorded"] == 0


def test_registry_restore_without_key_leaves_existing_ledger():
    registry = VisualRegistry(project_id="p1")
    ledger = tier_ladder.ledger_for(registry)
    ledger.spend_searches(4)
    registry.restore_state({"project_id": "p1"})
    assert tier_ladder.ledger_for(registry) is ledger
    assert ledger.searches == 4


def test_malformed_restore_never_raises_or_invents_spends():
    ledger = tier_ladder.EscalationLedger()
    ledger.spend_searches(3)
    # Non-dict data leaves the ledger unchanged (VisualRegistry
    # restore_state's contract).
    assert ledger.restore_state(None) is ledger
    assert ledger.searches == 3
    ledger.restore_state("not a dict")
    assert ledger.searches == 3
    # Empty / malformed pieces fall back to fresh, never invented.
    fresh = tier_ladder.EscalationLedger().restore_state({})
    assert fresh.summary()["extra_searches"] == 0
    weird = tier_ladder.EscalationLedger().restore_state(
        {"searches": "x", "downloads": None, "escalation_seconds": "y",
         "need_searches": "garbage", "attempts": [{"bad": 1}, ["x"],
                                                   [["k"], "not-a-dict"]]}
    )
    assert weird.searches == 0
    assert weird.downloads == 0
    assert weird.escalation_seconds == 0.0
    assert weird.attempts_recorded() == 0
    # An int-able total IS restored exactly — double-spend
    # protection depends on totals surviving.
    partial = tier_ladder.EscalationLedger().restore_state({"searches": 7})
    assert partial.searches == 7
    assert partial.allocate_searches(100) == (
        tier_ladder.PRODUCTION_MAX_EXTRA_SEARCHES - 7
    )
