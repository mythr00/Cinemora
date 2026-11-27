"""Transformation Phase 2.3 — structured script facts, KB
corroboration, and fact-based graphics.

- Extraction on the real Petito script (the mandate's test film
  1): >= 5 dated events, each traceable to its sentence, every
  fact stamped script_claim with its sentence reference.
- Corroboration (stub lookup only — the suite never calls the
  live KB): a fact upgrades to research_supported ONLY when the
  recorded KB verdict's description contains the fact's year,
  with the KB source recorded; anything else stays script_claim
  with the check recorded.
- The evidence producer's fact fallback and the Director's basis
  decisions (including the no-silent-upgrade rule).
- The chronology graphic renders from the extracted events with
  the fact basis visible in the graphics manifest; a script with
  no dates produces zero chronology graphics (AM-001).
- A fact-based stat graphic carries its recorded basis through
  the graphics pipeline.
"""

import json

import pytest

from services import evidence_producer
from services import graphics_pipeline
from services import kb_client
from services import script_facts
from services import visual_director

# The Petito production script verbatim (project
# 31df4571-c8e1-4bba-be20-fa3a48e2c87f, plan.json sentences).
PETITO_TEXTS = [
    "In the summer of 2021, a white van crossed America, carrying a dream a generation shared.",
    "Behind the camera was 22-year-old Gabby Petito, building a life of travel, one national park at a time.",
    "By September, the van would come home to Florida.",
    "Gabby would not.",
    "Gabby grew up in Blue Point, New York.",
    "In July 2021, she set out with her fiancé, Brian Laundrie, on a four-month journey through the American West, filming the trip as they went.",
    "On August 12, police in Moab, Utah, stopped the van after a report of a fight between the couple.",
    "The bodycam footage showed Gabby in tears.",
    "Officers separated them for the night, and let them go.",
    "Two weeks later, near Grand Teton National Park in Wyoming, the messages home stopped making sense.",
    "The last text, on August 30, read: \"No service in Yosemite\" — a park hundreds of miles from where they were.",
    "Her family believes she never wrote it.",
    "On September 1, Brian Laundrie arrived at his parents' home in North Port, Florida, driving Gabby's van.",
    "Gabby was not reported missing for another ten days.",
    "On September 11, her family reported her missing.",
    "The search centred on the Spread Creek camping area in Bridger-Teton National Forest, where the van had been seen in late August.",
    "On September 19, searchers found human remains.",
    "Two days later, they were confirmed to be Gabby.",
    "The coroner ruled her death a homicide.",
    "Laundrie, named a person of interest, had already vanished into a Florida nature reserve.",
    "His remains were found there in October, beside a notebook in which, the FBI later said, he claimed responsibility.",
    "Gabby Petito never got a trial, or a verdict.",
    "She got a country that refused to stop saying her name.",
]
PETITO_SENTENCES = [
    {"sentence_number": i + 1, "text": text}
    for i, text in enumerate(PETITO_TEXTS)
]
PETITO_BIBLE = {
    "entities": [
        {"id": "gabby_petito", "canonical": "Gabby Petito",
         "aliases": ["Gabby"], "type": "person"},
        {"id": "brian_laundrie", "canonical": "Brian Laundrie",
         "aliases": ["Laundrie"], "type": "person"},
    ],
    "primary_subject_id": "gabby_petito",
}


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_petito_extraction_yields_traceable_dated_events():
    table = script_facts.build_fact_table(PETITO_SENTENCES, bible=PETITO_BIBLE)
    record = table["record"]
    assert record["sentences"] == 23
    assert record["dated_events"] >= 5
    events = [f for f in table["facts"] if f["kind"] == "dated_event"]
    for fact in table["facts"]:
        # Every fact: the honest basis + its sentence reference.
        assert fact["basis"] == "script_claim"
        assert fact["source"] == {
            "type": "script",
            "sentence_number": fact["sentence_number"],
        }
        assert PETITO_TEXTS[fact["sentence_number"] - 1] == (
            fact.get("what") or PETITO_TEXTS[fact["sentence_number"] - 1]
        )
    by_sentence = {f["sentence_number"]: f for f in events}
    assert by_sentence[1]["when"] == "summer of 2021"
    assert by_sentence[1]["year"] == 2021
    assert by_sentence[6]["when"] == "July 2021"
    assert by_sentence[7]["when"] == "August 12"
    assert by_sentence[15]["when"] == "September 11"
    # Subjects come from the bible's entities, for corroboration
    # (sentence 6 names Brian Laundrie; Gabby is only "she" there).
    assert "Brian Laundrie" in by_sentence[6]["subjects"]


def test_petito_quantities_extracted():
    facts = script_facts.extract_script_facts(PETITO_SENTENCES)
    quantities = {
        (f["sentence_number"], f["display"])
        for f in facts if f["kind"] == "quantity"
    }
    assert (2, "22-year-old") in quantities
    assert (6, "four-month") in quantities


def test_timeline_events_carry_provenance():
    table = script_facts.build_fact_table(PETITO_SENTENCES, bible=PETITO_BIBLE)
    events = script_facts.timeline_events_from_facts(table["facts"])
    assert len(events) >= 5
    for event in events:
        assert event["fact_basis"] == "script_claim"
        assert event["support"] == "script_asserted"
        assert event["fact_id"]
        assert isinstance(event["sentence_number"], int)
        # Traceable: the event's text IS its source sentence.
        assert event["what"] == PETITO_TEXTS[event["sentence_number"] - 1]


def test_merge_does_not_duplicate_planner_events():
    table = script_facts.build_fact_table(PETITO_SENTENCES, bible=PETITO_BIBLE)
    existing = [{
        "when": "August 12, 2021",
        "what": (
            "Police in Moab, Utah, stopped the van after a report "
            "of a fight between the couple; officers separated "
            "them for the night and let them go."
        ),
        "support": "externally_supported",
    }]
    merged = script_facts.merge_timeline_events(existing, table["facts"])
    august_12 = [e for e in merged if "August 12" in str(e["when"])]
    assert len(august_12) == 1
    assert august_12[0]["support"] == "externally_supported"
    # The other extracted events were added.
    assert len(merged) > len(existing)


def test_dateless_script_produces_no_events():
    sentences = [
        {"sentence_number": 1, "text": "A quiet film about bread making."},
        {"sentence_number": 2, "text": "The village wakes early."},
    ]
    table = script_facts.build_fact_table(sentences)
    assert table["record"]["dated_events"] == 0
    assert script_facts.timeline_events_from_facts(table["facts"]) == []


# ---------------------------------------------------------------------------
# Corroboration — stub lookup, never the network
# ---------------------------------------------------------------------------

def _verdict(description, source="wikidata"):
    return {
        "status": "live", "type": "person", "confidence": 0.95,
        "type_source": source, "description": description,
        "aliases": [], "evidence": "verified",
        "signals": ["wikidata:p31"],
    }


def test_corroboration_upgrades_only_on_year_support():
    table = script_facts.build_fact_table(PETITO_SENTENCES, bible=PETITO_BIBLE)
    facts = table["facts"]

    def lookup(name, budget=None):
        # The KB description supports the 2021 dates.
        return _verdict("American traveller who disappeared in 2021")

    record = script_facts.corroborate_facts(facts, lookup=lookup)
    assert record["corroborated"] >= 1
    upgraded = [
        f for f in facts if f["basis"] == "research_supported"
    ]
    assert upgraded
    for fact in upgraded:
        assert fact["source"]["type"] == "kb"
        assert fact["source"]["source"] == "wikidata"
        assert fact["kb_check"]["status"] == "live"
    # Facts whose year the description does not contain (the
    # year-less month dates) stay script_claim.
    yearless = [
        f for f in facts
        if f["kind"] == "dated_event" and not f["year"]
    ]
    assert all(f["basis"] == "script_claim" for f in yearless)


def test_corroboration_without_year_in_description_upgrades_nothing():
    table = script_facts.build_fact_table(PETITO_SENTENCES, bible=PETITO_BIBLE)
    facts = table["facts"]

    def lookup(name, budget=None):
        return _verdict("American traveller and vlogger")

    record = script_facts.corroborate_facts(facts, lookup=lookup)
    assert record["corroborated"] == 0
    assert record["checked"] >= 1
    assert all(f["basis"] == "script_claim" for f in facts)
    # The check itself is recorded on the checked facts.
    checked = [f for f in facts if f.get("kb_check")]
    assert checked
    assert checked[0]["kb_check"]["query"] in (
        "Gabby Petito", "Brian Laundrie", "Laundrie",
    )


def test_kb_gate_is_open_in_the_shipped_state():
    # D-2 taken 2026-11-22 (Transformation Plan §2.3).
    assert kb_client.FOUNDER_D2_APPROVED is True
    assert kb_client.network_enabled() is True


# ---------------------------------------------------------------------------
# Evidence producer fact fallback + Director basis decisions
# ---------------------------------------------------------------------------

def _starved_outcome(sentence_number, scene_number=1):
    return {
        sentence_number: {
            "starved": True, "scene_number": scene_number,
            "sentence_number": sentence_number,
        }
    }


def test_evidence_producer_script_fact_fallback():
    table = script_facts.build_fact_table(PETITO_SENTENCES, bible=PETITO_BIBLE)
    # Sentence 2 is starved and has no verified asset anywhere:
    # its own extracted quantity serves, stamped script_claim.
    evidence, record = evidence_producer.build_evidence(
        [], _starved_outcome(2), fact_table=table
    )
    assert record["produced_script_fact"] == 1
    assert record["produced"] == 0
    entry = evidence[2]
    assert entry["fact_basis"] == "script_claim"
    assert entry["verified_data"]["display"] == "22-year-old"
    assert entry["evidence_ref"]["fact_id"]
    assert entry["evidence_ref"]["sentence_number"] == 2


def test_evidence_producer_kb_fact_fallback():
    table = script_facts.build_fact_table(PETITO_SENTENCES, bible=PETITO_BIBLE)
    script_facts.corroborate_facts(
        table["facts"],
        lookup=lambda name, budget=None: _verdict(
            "American traveller who disappeared in 2021"
        ),
    )
    evidence, record = evidence_producer.build_evidence(
        [], _starved_outcome(6), fact_table=table
    )
    assert record["produced_kb_fact"] == 1
    entry = evidence[6]
    assert entry["fact_basis"] == "research_supported"
    assert entry["evidence_ref"]["kb_source"] == "wikidata"


def test_evidence_producer_without_fact_table_is_unchanged():
    evidence, record = evidence_producer.build_evidence(
        [], _starved_outcome(2)
    )
    assert evidence == {}
    assert record["no_verified_asset_in_scene"] == 1
    assert record["produced_script_fact"] == 0


def _reconcile(evidence_entry, sentence_number=2):
    plan = {
        "version": 1, "project_id": "p2-facts",
        "sentences": [{
            "sentence_number": sentence_number,
            "primary_visual_intent": "graphic_explanation",
            "scene_number": 1, "scene_index": 0,
        }],
    }
    outcomes = {
        sentence_number: {
            "starved": True, "scene_number": 1, "scene_index": 0,
            "sentence_number": sentence_number,
            "text": PETITO_TEXTS[sentence_number - 1],
            "scenario": {},
        }
    }
    _plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, evidence={sentence_number: evidence_entry}
    )
    return manifest["entries"][0]


def test_director_stamps_kb_fact_as_research_supported():
    entry = _reconcile({
        "evidence_ref": {
            "fact_id": "sf_0007", "source": "script_facts",
            "sentence_number": 2, "kb_source": "wikidata",
            "kb_query": "Gabby Petito",
        },
        "verified_data": {"display": "22-year-old", "kind": "script_quantity"},
        "fact_basis": "research_supported",
    })
    assert entry["fact_basis"] == "research_supported"
    assert entry["verified_data"]["display"] == "22-year-old"
    assert entry["evidence_ref"]["kb_source"] == "wikidata"
    # P0 removal (2026-11-27): the Director used to add the
    # native stat family for a research-supported fact; that
    # family is removed completely, so the basis still stamps
    # (above) but "stat" is never a family candidate.
    assert "stat" not in entry["family_candidates"]


def test_director_never_promotes_a_fact_without_kb_source():
    # The producer claims research_supported but the ref carries
    # no KB source: the Director downgrades to script_claim.
    entry = _reconcile({
        "evidence_ref": {
            "fact_id": "sf_0007", "source": "script_facts",
            "sentence_number": 2,
        },
        "verified_data": {"display": "22-year-old", "kind": "script_quantity"},
        "fact_basis": "research_supported",
    })
    assert entry["fact_basis"] == "script_claim"
    # The data still rides through — as the script claim it is.
    assert entry["verified_data"]["display"] == "22-year-old"


# ---------------------------------------------------------------------------
# Graphics: the chronology renders from facts; the stat carries its basis
# ---------------------------------------------------------------------------

def _graphics_data(timeline):
    return {
        "case_bible": {"title": "Gabby Petito", "timeline": timeline},
        "scenes": [{
            "scene_number": 1,
            "sentences": [
                {"text": t, "sentence_number": i + 1}
                for i, t in enumerate(PETITO_TEXTS[:3])
            ],
        }],
    }


def _manifest(entries):
    return {"version": 1, "project_id": "p2-facts", "theme": None,
            "entries": entries}


def _entry(manifest_id, scene, index, number, text, families, **extra):
    entry = {
        "manifest_id": manifest_id,
        "target": {
            "scene_number": scene, "sentence_index": index,
            "sentence_number": number,
        },
        "text": text, "scenario": {}, "purpose": "coverage_fallback",
        "template_candidates": [], "family_candidates": families,
        "theme": None, "fact_basis": "script_claim",
        "evidence_ref": None, "verified_data": None,
    }
    entry.update(extra)
    return entry


# P0 removal (2026-11-27): test_chronology_renders_from_
# extracted_events deleted — the chronology (timeline) graphic
# is removed completely (R1); extracted events no longer render
# as a graphic. The fact-table event extraction itself is
# pinned elsewhere in this file.


def test_dateless_script_produces_zero_chronology(tmp_path):
    entry = _entry(
        "gm_0001", 1, 0, 1, "A quiet film about bread making.",
        families=["timeline"],
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _graphics_data([]), tmp_path, mode="balanced",
        manifest=_manifest([entry]),
    )
    assert [a for a in assets if a["graphic_type"] == "timeline"] == []


def _fake_renders(monkeypatch):
    """The Phase 6 logic-test pattern: fake file-writing renderers
    + the validator stubbed at its seam (the real-render path is
    covered by test_chronology_renders_from_extracted_events)."""

    def fake_write(path, *args, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)

    def fake_timeline(path, *args, **kwargs):
        fake_write(path)
        return "proportional"

    monkeypatch.setattr(
        graphics_pipeline, "validate_graphic_output",
        lambda path, expected_duration=None, **kwargs: {
            "passed": True, "failure_kind": None, "detail": "",
            "checks": {}, "probe": None,
        },
    )


def test_fact_stat_carries_research_basis(tmp_path, monkeypatch):
    """P0 removal pin (rewritten): a research-supported fact with
    a KB source used to ride the native stat family onto a card.
    The family is removed completely — the entry now produces NO
    graphic; the verified display is never promoted onto a card
    that no longer exists."""
    _fake_renders(monkeypatch)
    entry = _entry(
        "gm_0001", 1, 1, 2, PETITO_TEXTS[1], families=["stat"],
        fact_basis="research_supported",
        evidence_ref={
            "fact_id": "sf_0003", "source": "script_facts",
            "sentence_number": 2, "kb_source": "wikidata",
            "kb_query": "Gabby Petito",
        },
        verified_data={"display": "22-YEAR-OLD", "kind": "script_quantity"},
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _graphics_data([]), tmp_path, mode="balanced",
        manifest=_manifest([entry]),
    )
    assert assets == []


def test_fact_stat_without_kb_source_is_downgraded(tmp_path, monkeypatch):
    """P0 removal pin (rewritten): without a KB source the fact
    was downgraded to a script claim and still rendered as a
    stat. The stat family is gone, so the entry produces NO
    graphic either way."""
    _fake_renders(monkeypatch)
    entry = _entry(
        "gm_0001", 1, 1, 2, PETITO_TEXTS[1], families=["stat"],
        fact_basis="research_supported",
        evidence_ref={
            "fact_id": "sf_0003", "source": "script_facts",
            "sentence_number": 2,
        },
        verified_data={"display": "22-YEAR-OLD", "kind": "script_quantity"},
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _graphics_data([]), tmp_path, mode="balanced",
        manifest=_manifest([entry]),
    )
    assert assets == []
