"""Phase 8 — AM-009 live evidence producer.

Phase 6 built the verified asset-data path (Director evidence ->
manifest -> stat maker) but no production caller assembled the
evidence. The producer (services/evidence_producer.py) is that
caller: it maps recorded structured facts from a production's own
VERIFIED assets onto its starved sentences, under the register's
guard — VERIFIED only on an exact recorded ref, same scene,
verbatim values, identity required.

These tests drive the exact jobs call shape: build_evidence over
processed scenes + Director outcomes, reconcile, then the real
graphics pipeline over the resulting manifest.
"""

import pytest

from services import evidence_producer, graphics_pipeline, visual_director


def _asset(**overrides):
    asset = {
        "type": "image",
        "file_path": "/media/verified.jpg",
        "title": "Survey photograph",
        "candidate_id": "cand_survey_1",
        "source": "wikimedia",
        "source_url": "https://commons.wikimedia.org/wiki/File:Survey.jpg",
        "verification": {"accepted": True},
        "metadata_facts": [
            {"display": "140 FEET", "kind": "length"},
        ],
    }
    asset.update(overrides)
    return asset


def _processed(assets, missing=(2,)):
    return [{
        "scene": {
            "scene_number": 1,
            "sentences": [
                {"sentence_number": 1, "text": "Guards watched."},
                {"sentence_number": 2,
                 "text": "The tunnel ran beneath the wall."},
            ],
        },
        "asset_stats": {
            "missing_visuals": [
                {"sentence_number": n} for n in missing
            ],
        },
        "videos": [],
        "images": assets,
    }]


def _outcomes(processed):
    return visual_director.outcomes_from_processed_scenes(processed)


def _plan_v1():
    rows = []
    for number in (1, 2):
        rows.append({
            "sentence_number": number, "scene_number": 1,
            "role": "statement", "primary_visual_intent": "exact",
            "secondary_visual_intents": [], "intent_queries": {},
            "graphic_fallback": {"allowed": True, "kind": "stat"},
            "preferred_media_type": "video",
            "specificity_requirement": "exact",
            "visual_misrepresentation_risk": "low",
            "intents_source": "llm_plan", "fallback_concepts": None,
            "concepts_source": None, "entry_tier": "direct",
        })
    return {"version": 1, "project_id": "p8", "analysis_mode": "llm",
            "scene_governor": None, "sentences": rows}


def test_producer_maps_recorded_fact_to_starved_sentence():
    processed = _processed([_asset()])
    evidence, record = evidence_producer.build_evidence(
        processed, _outcomes(processed)
    )
    assert record == {
        "starved_sentences": 1, "produced": 1,
        # Phase 2.3: the script-fact fallback counters ride on the
        # record (zero here — the asset path produced).
        "produced_script_fact": 0, "produced_kb_fact": 0,
        "no_verified_asset_in_scene": 0, "no_recorded_fact": 0,
        "no_recorded_identity": 0,
    }
    entry = evidence[2]
    assert entry["evidence_ref"]["asset_id"] == "cand_survey_1"
    assert entry["evidence_ref"]["source"] == "wikimedia"
    assert entry["verified_data"] == {
        "display": "140 FEET", "kind": "length",
    }


def test_producer_ignores_covered_sentences_and_other_scenes():
    # The fact-bearing asset sits in scene 2; the starved sentence
    # is in scene 1 — no cross-scene borrowing.
    scene_one = _processed([])[0]
    scene_two = {
        "scene": {
            "scene_number": 2,
            "sentences": [
                {"sentence_number": 3, "text": "Later, elsewhere."},
            ],
        },
        "asset_stats": {"missing_visuals": []},
        "videos": [],
        "images": [_asset()],
    }
    processed = [scene_one, scene_two]
    evidence, record = evidence_producer.build_evidence(
        processed, _outcomes(processed)
    )
    assert evidence == {}
    assert record["produced"] == 0
    assert record["no_verified_asset_in_scene"] == 1


def test_producer_no_fact_no_identity_and_unverified_cases():
    # Verified asset without recorded facts.
    processed = _processed([_asset(metadata_facts=None)])
    evidence, record = evidence_producer.build_evidence(
        processed, _outcomes(processed)
    )
    assert evidence == {}
    assert record["no_recorded_fact"] == 1

    # Fact present but the asset carries no recorded identity.
    processed = _processed([_asset(candidate_id=None)])
    evidence, record = evidence_producer.build_evidence(
        processed, _outcomes(processed)
    )
    assert evidence == {}
    assert record["no_recorded_identity"] == 1

    # Asset not verification-accepted: never eligible.
    processed = _processed([
        _asset(verification={"accepted": False}),
    ])
    evidence, record = evidence_producer.build_evidence(
        processed, _outcomes(processed)
    )
    assert evidence == {}
    assert record["no_verified_asset_in_scene"] == 1

    # Probed technical facts are NOT subject facts: an asset with
    # only technical_checks produces nothing.
    technical_only = _asset(metadata_facts=None)
    technical_only["technical_checks"] = {
        "passed": True,
        "facts": {"width": 5472, "height": 3648},
    }
    processed = _processed([technical_only])
    evidence, record = evidence_producer.build_evidence(
        processed, _outcomes(processed)
    )
    assert evidence == {}
    assert record["no_recorded_fact"] == 1


def test_producer_is_fail_soft_on_garbage():
    evidence, record = evidence_producer.build_evidence(
        [{"scene": None, "videos": "junk"}], {1: {"starved": True}}
    )
    assert evidence == {}
    assert record["starved_sentences"] == 1


def _fake_renders(monkeypatch):
    def fake_write(path, *args, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)

    # P0 removal (2026-11-27): the native stat renderer this
    # fixture used to fake is removed; the validator seam remains.
    monkeypatch.setattr(
        graphics_pipeline, "validate_graphic_output",
        lambda path, expected_duration=None, **kwargs: {
            "passed": True, "failure_kind": None, "detail": "",
            "checks": {}, "probe": None,
        },
    )


def test_end_to_end_verified_graphic_from_producer(tmp_path, monkeypatch):
    """The jobs call shape, end to end: producer -> Director ->
    manifest -> graphics pipeline. P0 removal (2026-11-27): the
    verified-data path used to end in a native stat graphic
    stamped verified_asset_metadata; that family is removed
    completely, so the Director still stamps the entry (below)
    but the pipeline produces NO graphic from it — the verified
    display is never promoted onto a card."""
    _fake_renders(monkeypatch)
    processed = _processed([_asset()])
    outcomes = _outcomes(processed)
    evidence, record = evidence_producer.build_evidence(
        processed, outcomes
    )
    plan_v2, manifest = visual_director.reconcile_visual_plan(
        _plan_v1(), outcomes, evidence=evidence,
    )
    entry = manifest["entries"][0]
    assert entry["fact_basis"] == "verified_asset_metadata"
    assert entry["evidence_ref"]["asset_id"] == "cand_survey_1"

    data = {
        "case_bible": {"timeline": []},
        "scenes": [{
            "scene_number": 1,
            "sentences": [
                {"text": "Guards watched.", "sentence_number": 1},
                {"text": "The tunnel ran beneath the wall.",
                 "sentence_number": 2},
            ],
        }],
    }
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=manifest,
    )
    assert assets == []
    assert all("stat_display" not in a for a in assets)


def test_end_to_end_without_fact_stays_script_claim(tmp_path, monkeypatch):
    """Same production, no recorded fact on the verified asset:
    the graphic (if any) is built from the script and stays
    script_claim — the producer changes nothing."""
    _fake_renders(monkeypatch)
    processed = _processed([_asset(metadata_facts=None)])
    outcomes = _outcomes(processed)
    evidence, _record = evidence_producer.build_evidence(
        processed, outcomes
    )
    assert evidence == {}
    _plan_v2, manifest = visual_director.reconcile_visual_plan(
        _plan_v1(), outcomes, evidence=evidence,
    )
    assert manifest["entries"][0]["fact_basis"] == "script_claim"
