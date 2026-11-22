"""Phase 7 — the KB stage, founder decision D-2 now TAKEN.

kb_client was built as architecture (cache, budgets, timeout and
malformed handling, verified-vs-inferred provenance) behind the
D-2 gate. The decision was taken on 2026-11-22 under the Complete
Autonomous Takeover Mandate (Transformation Plan §2.3), so the
shipped state is network ENABLED. These tests pin: the shipped
gate state; the disabled behaviour, exercised by monkeypatching
the gate closed (zero outbound calls, "kb_disabled" recorded);
and the live path, exercised only through an injected transport.
The suite itself never performs a real lookup — conftest stubs
the default transport to unreachable.
"""

import socket

import pytest

from services import degraded_planner
from services import kb_client


@pytest.fixture(autouse=True)
def _isolated_intel_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_INTEL_DB", str(tmp_path / "intel.sqlite3"))


def test_network_is_enabled_by_the_d2_decision():
    # D-2 taken 2026-11-22 under the takeover mandate
    # (Transformation Plan §2.3): the shipped gate state is open.
    assert kb_client.FOUNDER_D2_APPROVED is True
    assert kb_client.network_enabled() is True


def test_lookup_records_kb_disabled_and_never_calls_transport(monkeypatch):
    monkeypatch.setattr(kb_client, "FOUNDER_D2_APPROVED", False)
    calls = []

    def spy_transport(url, params):
        calls.append((url, params))
        raise AssertionError("transport must not be called while D-2 is unmade")

    result = kb_client.lookup_entity("Zxqv Blorp Entity", transport=spy_transport)
    assert result["status"] == "disabled"
    assert result["type"] is None
    assert "kb_disabled" in result["signals"]
    assert calls == []


def test_lookup_makes_zero_network_calls(monkeypatch):
    monkeypatch.setattr(kb_client, "FOUNDER_D2_APPROVED", False)

    def _blocked_connect(self, *args, **kwargs):
        raise AssertionError("a network connection was attempted")

    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    result = kb_client.lookup_entity("Another Unseen Name")
    assert result["status"] == "disabled"
    assert "kb_disabled" in result["signals"]


def test_bible_records_the_disabled_kb_stage(monkeypatch):
    monkeypatch.setattr(kb_client, "FOUNDER_D2_APPROVED", False)
    bible = degraded_planner.build_bible([
        "Marie Curie won two Nobel Prizes.",
        "She worked in Paris for decades.",
    ])
    assert bible["kb"]["network_enabled"] is False
    assert bible["kb"]["live"] == 0
    # Every entity's typing signals record that the KB stage was
    # consulted and found disabled — never silently skipped.
    assert all(
        "kb_disabled" in entity["type_signals"] for entity in bible["entities"]
    )
    # Entities the cascade cannot type say WHY: the KB that would
    # have answered is disabled (D-2 evidence, recorded per entity).
    untyped = [e for e in bible["entities"] if e["type"] == "other"]
    assert untyped and all(
        e["type_source"] == "kb_disabled" for e in untyped
    )


def test_bible_records_the_enabled_kb_stage():
    # Shipped state (D-2 taken): the bible records the stage as
    # enabled. The suite's default transport is stubbed offline
    # (conftest), so lookups record unreachable and typing falls
    # through to the deterministic stages — the same bible an
    # offline production box would build.
    bible = degraded_planner.build_bible([
        "Marie Curie won two Nobel Prizes.",
        "She worked in Paris for decades.",
    ])
    assert bible["kb"]["network_enabled"] is True
    assert bible["kb"]["live"] == 0
    assert all(
        "kb_disabled" not in entity["type_signals"]
        for entity in bible["entities"]
    )


# ------------------------------------------------------------------
# The architecture behind the gate (injected transport only)
# ------------------------------------------------------------------

_WIKIDATA_PERSON = {
    "search": [{"id": "Q12345"}],
    "entity": {
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]
        },
        "descriptions": {"en": {"value": "test person"}},
        "aliases": {"en": [{"value": "T P"}]},
    },
}


def _fake_transport(calls):
    def transport(url, params):
        calls.append(url)
        if params.get("action") == "wbsearchentities":
            return {"search": _WIKIDATA_PERSON["search"]}
        if params.get("action") == "wbgetentities":
            return {"entities": {"Q12345": _WIKIDATA_PERSON["entity"]}}
        return {}

    return transport


def test_live_path_architecture_with_injected_transport(monkeypatch):
    monkeypatch.setattr(kb_client, "FOUNDER_D2_APPROVED", True)
    calls = []
    result = kb_client.lookup_entity(
        "Test Person", transport=_fake_transport(calls)
    )
    assert result["status"] == "live"
    assert result["type"] == "person"  # P31 Q5 -> person
    assert result["confidence"] == kb_client.CONFIDENCE_WIKIDATA
    assert result["type_source"] == "wikidata"
    # A KB verdict is a VERIFIED fact, distinct from inferred types.
    assert result["evidence"] == "verified"
    assert result["aliases"] == ["T P"]

    # The verdict is cached: a second lookup pays zero calls.
    calls.clear()
    again = kb_client.lookup_entity(
        "Test Person", transport=_fake_transport(calls)
    )
    assert again["status"] == "cache_hit"
    assert again["type"] == "person"
    assert calls == []


def test_live_path_malformed_responses_are_statuses_not_crashes(monkeypatch):
    monkeypatch.setattr(kb_client, "FOUNDER_D2_APPROVED", True)

    def bad_transport(url, params):
        return ["not", "a", "dict"]

    result = kb_client.lookup_entity("Broken Source", transport=bad_transport)
    assert result["status"] == "malformed"
    assert result["type"] is None


def test_live_path_unreachable_is_a_status_not_a_crash(monkeypatch):
    monkeypatch.setattr(kb_client, "FOUNDER_D2_APPROVED", True)

    def dead_transport(url, params):
        raise ConnectionError("no route")

    result = kb_client.lookup_entity("Far Away", transport=dead_transport)
    assert result["status"] == "unreachable"
    assert result["type"] is None


def test_live_path_budget_exhaustion_is_recorded(monkeypatch):
    monkeypatch.setattr(kb_client, "FOUNDER_D2_APPROVED", True)
    result = kb_client.lookup_entity(
        "Costly Name",
        budget=kb_client.KBBudget(max_calls=0),
        transport=_fake_transport([]),
    )
    assert result["status"] == "budget_exhausted"
