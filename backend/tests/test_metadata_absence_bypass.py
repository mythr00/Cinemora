"""Metadata-absence bypass (run-6345 fix, 2026-11-23).

Run 6345a1d7 (Hyatt Regency engineering documentary) evaluated 543
candidates and rejected 506; the metadata gate alone rejected 357,
311 of them on ABSENCE primaries (missing_place 166, missing_subject
124, missing_person 3, no_topical_evidence 18) — candidates whose
metadata did not literally mention the planner's required place /
subject, which is an absence of evidence, not evidence of
wrongness. Only 8 downloads were ever attempted and 21 of 25
sentences fell through to generated graphics.

These tests pin the fix's contract:

* a candidate whose GATE failures are absence-only is scored, and
  when the scorer accepts it on positive evidence it is admitted
  SPECULATIVELY: recorded penalty, bypass marker + absence reasons
  on the item and the verdict, eligible for the pick;
* any contradiction anywhere (gate or scorer) still hard-rejects;
* the scorer's own absence verdicts (no_topical_evidence /
  missing_required_subject) and its threshold still reject — the
  bypass rescues unmentioned entities, not zero-evidence
  candidates (the VA-04 guarantee is untouched);
* speculative downloads are budget-capped: per beat and per
  production, sharing the D-B2 extra-download account (≤20) with
  tier-escalation downloads;
* a speculative asset must be CONFIRMED by frame-level
  verification after download — a frame-stage abstention demotes
  it with the typed speculative_frame_unconfirmed reason, while
  non-speculative assets keep the historical fail-soft semantics;
* the bypass marker reaches the decision ledger's rows.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services import media_pipeline as mp
from services import pipeline_verification
from services import rejection_diagnostics as rd
from services import run_ledger
from services import story_understanding as story
from services import tier_ladder
from services.visual_registry import VisualRegistry


@pytest.fixture(autouse=True)
def _clean_provider_state():
    # Provider health is SHARED state (Redis, Phase 8.4): another
    # test file's simulated 429s can leave this file's fetch hosts
    # backed off platform-wide for minutes, refusing fetches before
    # any request — and provider_health.reset() deliberately does
    # not clear the shared backoff. The Phase 8 test seam
    # (test_phase8_concurrency) is the isolation: a fakeredis
    # client for the duration of each test here. reset_media_caches
    # additionally clears the failed-download set and the per-run
    # observation table, as in production.
    import fakeredis

    from services import provider_health

    provider_health.set_redis_client(
        fakeredis.FakeRedis(decode_responses=True)
    )
    mp.reset_media_caches()
    yield
    mp.reset_media_caches()
    provider_health.reset_redis_client()


def _bible():
    return story.hydrate_bible({
        "entities": [
            {"id": "berlin_wall", "canonical": "Berlin Wall", "aliases": ["the Wall"], "type": "object"},
            {"id": "east_berlin", "canonical": "East Berlin", "aliases": [], "type": "location"},
        ]
    })


def _scenario(**over):
    sc = {
        "subject_id": "berlin_wall",
        "specificity": "representative",
        "search_terms": ["crowds crossing checkpoint"],
        "visual": "crowds of people crossing through an opened checkpoint",
        "era": "1989",
    }
    sc.update(over)
    return sc


def _need(**over):
    need = {
        "required_type": "event",
        "specificity": "representative",
        "visual_category": "evidence",
        "required_subject": "East Berlin",
        "place": "berlin",
    }
    need.update(over)
    return need


# A candidate with real subject + topical evidence whose metadata
# never names the need's required subject entity ("East Berlin"):
# the gate fails it missing_subject — absence only.
_SPECULATIVE_CANDIDATE = {
    "title": "Berlin Wall crowds crossing checkpoint 1989",
    "snippet": "Crowds of people crossing through the opened checkpoint",
    "url": "https://media.example.org/wall-crowds",
}


# ---------------------------------------------------------------------------
# Classification vocabulary
# ---------------------------------------------------------------------------

def test_absence_classification_is_exact_and_fails_closed():
    assert rd.failures_are_absence_only(["missing_place"])
    assert rd.failures_are_absence_only(
        ["missing_subject", "missing_place", "no_topical_evidence"]
    )
    # Any contradiction (or unclassified reason) in the list blocks
    # the bypass; an empty list is not an absence failure at all.
    assert not rd.failures_are_absence_only(
        ["missing_subject", "contradictory_date"]
    )
    assert not rd.failures_are_absence_only(["below_threshold"])
    assert not rd.failures_are_absence_only([])
    assert not rd.failures_are_absence_only(None)
    # The two classes are disjoint and cover the gate's vocabulary.
    assert not (rd.ABSENCE_REASONS & rd.CONTRADICTION_REASONS)
    for reason in (
        "missing_place", "missing_subject", "missing_person",
        "missing_required_subject", "no_topical_evidence",
    ):
        assert reason in rd.ABSENCE_REASONS
    for reason in (
        "contradictory_place", "contradictory_date",
        "contradictory_geography", "contradictory_event",
        "contradictory_person", "unrelated_named_person",
        "social_platform_source",
    ):
        assert reason in rd.CONTRADICTION_REASONS


def test_bypass_reasons_are_registered():
    assert rd.is_registered_reason(rd.SPECULATIVE_DOWNLOAD_BUDGET_REASON)
    assert rd.is_registered_reason(rd.SPECULATIVE_FRAME_UNCONFIRMED_REASON)


# ---------------------------------------------------------------------------
# _uve_verify_all: the bypass decision
# ---------------------------------------------------------------------------

def test_absence_only_gate_failure_becomes_speculative():
    scored = mp._uve_verify_all(
        [dict(_SPECULATIVE_CANDIDATE)], _scenario(), _bible(),
        shot_need=_need(),
    )
    assert len(scored) == 1
    item = scored[0]
    # Not rejected anywhere: admitted to the pool as speculative.
    assert not item.get("reject_reason")
    assert item.get("metadata_absence_bypass") is True
    assert item.get("absence_reasons") == ["missing_subject"]
    assert item.get("gate_failures") == ["missing_subject"]
    verdict = item["verification"]
    assert verdict["accepted"] is True
    assert verdict["metadata_absence_bypass"] is True
    assert verdict["absence_reasons"] == ["missing_subject"]
    assert verdict["acceptance_basis"] == (
        "metadata_absence_bypass_frame_confirmation_required"
    )
    # The recorded penalty is applied to the score the pool ranks by.
    assert verdict["pre_penalty_score"] == 1.0
    assert verdict["score"] == pytest.approx(
        1.0 - mp.METADATA_ABSENCE_PENALTY
    )
    assert item["universal_score"] == verdict["score"]
    assert item["metadata_absence_penalty"] == mp.METADATA_ABSENCE_PENALTY


def test_speculative_candidate_is_pickable():
    scored = mp._uve_verify_all(
        [dict(_SPECULATIVE_CANDIDATE)], _scenario(), _bible(),
        shot_need=_need(),
    )
    picked = mp._uve_pick(
        scored, _scenario(), VisualRegistry(), set(), shot_need=_need()
    )
    assert len(picked) == 1
    assert picked[0]["metadata_absence_bypass"] is True


def test_gate_contradiction_still_hard_rejects_at_gate():
    # Era 1989 need; the candidate positively states 1961 — a date
    # contradiction riding alongside satisfiable subject/place.
    candidate = {
        "title": "East Berlin parade 1961",
        "snippet": "soldiers march through East Berlin in 1961",
        "url": "https://media.example.org/parade-1961",
    }
    scored = mp._uve_verify_all(
        [candidate], _scenario(), _bible(), shot_need=_need(era="1989")
    )
    item = scored[0]
    assert item["reject_reason"] == "contradictory_date"
    assert item["rejection_stage"] == "metadata_gate"
    assert not item.get("metadata_absence_bypass")
    assert item["verification"]["accepted"] is False


def test_mixed_absence_and_contradiction_still_hard_rejects():
    # missing_subject (absence) + contradictory_date in ONE gate
    # list: the contradiction decides, at the gate, as before.
    candidate = {
        "title": "Berlin parade 1961",
        "snippet": "soldiers march in 1961",
        "url": "https://media.example.org/parade-west",
    }
    scored = mp._uve_verify_all(
        [candidate], _scenario(), _bible(), shot_need=_need(era="1989")
    )
    item = scored[0]
    assert item["rejection_stage"] == "metadata_gate"
    assert item["reject_reason"] == "contradictory_date"
    assert set(item["gate_failures"]) == {
        "missing_subject", "contradictory_date",
    }
    assert not item.get("metadata_absence_bypass")


def test_scorer_hard_reject_stands_for_bypassed_candidate():
    # Gate absence-only (the metadata never names "East Berlin"),
    # but the scorer's social-platform rule hard-rejects the source
    # itself: the bypass never rescues a scorer hard reject.
    candidate = {
        "title": "Berlin Wall crowds crossing checkpoint 1989",
        "snippet": "Crowds of people crossing through the opened checkpoint",
        "url": "https://www.tiktok.com/@archive/video/123",
    }
    scored = mp._uve_verify_all(
        [candidate], _scenario(), _bible(), shot_need=_need()
    )
    item = scored[0]
    assert item["reject_reason"] == "social_platform_source"
    assert item["rejection_stage"] == "scoring"
    assert not item.get("metadata_absence_bypass")


def test_zero_evidence_candidate_still_rejected_at_scoring():
    # The Berlin run's champagne-wall spam: gate failures are
    # absence-only, but the metadata shares NOTHING with the topic,
    # so the scorer's no_topical_evidence verdict (VA-04) stands —
    # the bypass does not turn zero-evidence candidates speculative.
    candidate = {
        "title": "Champagne wall rental",
        "snippet": "weddings",
        "url": "https://media.example.org/champagne",
    }
    scored = mp._uve_verify_all(
        [candidate], _scenario(), _bible(), shot_need=_need()
    )
    item = scored[0]
    assert item["reject_reason"] == "no_topical_evidence"
    assert item["rejection_stage"] == "scoring"
    assert not item.get("metadata_absence_bypass")


def test_below_threshold_candidate_still_rejected_at_scoring():
    # Some topical overlap (so not zero-evidence) but far too weak:
    # the scorer's threshold is untouched by the bypass.
    candidate = {
        "title": "Checkpoint cafe in Berlin",
        "snippet": "a checkpoint themed cafe near the crossing",
        "url": "https://media.example.org/cafe",
    }
    scored = mp._uve_verify_all(
        [candidate], _scenario(), _bible(), shot_need=_need()
    )
    item = scored[0]
    assert item["reject_reason"] == "below_threshold"
    assert item["rejection_stage"] == "scoring"
    assert not item.get("metadata_absence_bypass")


def test_gate_passing_candidate_is_not_marked_speculative():
    candidate = {
        "title": "East Berlin crowds crossing the Berlin Wall checkpoint 1989",
        "snippet": "Crowds of people crossing through the opened checkpoint in East Berlin",
        "url": "https://media.example.org/east-crowds",
    }
    scored = mp._uve_verify_all(
        [candidate], _scenario(), _bible(), shot_need=_need()
    )
    item = scored[0]
    assert item["verification"]["accepted"] is True
    assert not item.get("metadata_absence_bypass")
    assert "metadata_absence_bypass" not in item["verification"]


# ---------------------------------------------------------------------------
# Budget caps (tier_ladder ledger — the existing D-B2 machinery)
# ---------------------------------------------------------------------------

def test_speculative_downloads_share_the_production_account():
    ledger = tier_ladder.EscalationLedger()
    for _ in range(tier_ladder.PRODUCTION_MAX_EXTRA_DOWNLOADS - 2):
        ledger.spend_download()
    assert ledger.can_spend_speculative_download()
    ledger.spend_speculative_download()
    ledger.spend_speculative_download()
    # 18 escalation + 2 speculative = the D-B2 account of 20: both
    # routes now refuse — the cap is a production total, not per
    # route.
    assert not ledger.can_spend_speculative_download()
    assert not ledger.can_spend_download()
    assert ledger.summary()["speculative_downloads"] == 2
    assert ledger.summary()["extra_downloads"] == (
        tier_ladder.PRODUCTION_MAX_EXTRA_DOWNLOADS - 2
    )


def test_speculative_per_beat_cap():
    ledger = tier_ladder.EscalationLedger()
    beat = ("scene-dir", 7)
    for _ in range(tier_ladder.SPECULATIVE_PER_BEAT_DOWNLOAD_CAP):
        assert ledger.can_spend_speculative_download(beat_key=beat)
        ledger.spend_speculative_download(beat_key=beat)
    assert not ledger.can_spend_speculative_download(beat_key=beat)
    # Another beat still has its own allowance.
    assert ledger.can_spend_speculative_download(beat_key=("scene-dir", 8))


# ---------------------------------------------------------------------------
# _uve_fetch_first: budget gate + frame confirmation
# ---------------------------------------------------------------------------

def _fetch_candidate(url, **over):
    candidate = {
        "title": "Berlin Wall crowds",
        "url": url,
        "provider": "wikimedia",
        "candidate_id": f"cand-{url.rsplit('/', 1)[-1]}",
        "metadata_absence_bypass": True,
        "metadata_absence_penalty": mp.METADATA_ABSENCE_PENALTY,
        "absence_reasons": ["missing_subject"],
    }
    candidate.update(over)
    return candidate


def _fake_downloader_factory(tmp_path, calls):
    def _download(candidate, scene_dir, sentence_number, asset_index):
        calls.append(candidate["url"])
        path = tmp_path / f"asset-{len(calls)}.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 2048)
        return {
            "file_path": str(path),
            "type": "image",
            "url": candidate["url"],
            "title": candidate.get("title", ""),
            "provider": "wikimedia",
            "candidate_id": candidate.get("candidate_id"),
        }

    return _download


def _patch_frame_check(monkeypatch, outcome):
    def _check(asset, candidate, scenario, bible):
        return {"outcome": outcome, "reason": "", "stage": "frame_verification"}

    monkeypatch.setattr(
        pipeline_verification, "_apply_frame_check", _check
    )


def test_fetch_confirms_speculative_asset_on_frame_pass(tmp_path, monkeypatch):
    _patch_frame_check(monkeypatch, "pass")
    calls = []
    candidate = _fetch_candidate("https://media.example.org/spec-pass")
    failures = []
    asset = pipeline_verification._uve_fetch_first(
        [candidate], _fake_downloader_factory(tmp_path, calls),
        str(tmp_path), 7, 1, VisualRegistry(), set(),
        failures=failures,
    )
    assert asset is not None
    assert asset["speculative_frame_confirmed"] is True
    assert calls == ["https://media.example.org/spec-pass"]
    assert failures == []


def test_fetch_demotes_speculative_asset_when_frame_stage_abstains(
    tmp_path, monkeypatch
):
    _patch_frame_check(monkeypatch, "not_evaluated")
    calls = []
    candidate = _fetch_candidate("https://media.example.org/spec-abstain")
    failures = []
    asset = pipeline_verification._uve_fetch_first(
        [candidate], _fake_downloader_factory(tmp_path, calls),
        str(tmp_path), 7, 1, VisualRegistry(), set(),
        failures=failures,
    )
    # Downloaded, examined, and demoted: never silently accepted.
    assert asset is None
    assert calls == ["https://media.example.org/spec-abstain"]
    assert len(failures) == 1
    assert failures[0]["reason"] == rd.SPECULATIVE_FRAME_UNCONFIRMED_REASON
    assert failures[0]["stage"] == "frame_verification"


def test_fetch_keeps_failsoft_for_non_speculative_asset(tmp_path, monkeypatch):
    _patch_frame_check(monkeypatch, "not_evaluated")
    calls = []
    candidate = _fetch_candidate(
        "https://media.example.org/verified-plain",
        metadata_absence_bypass=False,
    )
    candidate.pop("metadata_absence_bypass")
    failures = []
    asset = pipeline_verification._uve_fetch_first(
        [candidate], _fake_downloader_factory(tmp_path, calls),
        str(tmp_path), 7, 1, VisualRegistry(), set(),
        failures=failures,
    )
    # A metadata-verified asset is untouched by a frame-stage
    # abstention — the historical fail-soft semantics.
    assert asset is not None
    assert not asset.get("speculative_frame_confirmed")
    assert failures == []


def test_fetch_enforces_per_beat_and_production_caps(tmp_path, monkeypatch):
    _patch_frame_check(monkeypatch, "not_evaluated")
    calls = []
    registry = VisualRegistry()
    downloader = _fake_downloader_factory(tmp_path, calls)
    candidates = [
        _fetch_candidate(f"https://media.example.org/spec-cap-{n}")
        for n in range(3)
    ]
    failures = []
    asset = pipeline_verification._uve_fetch_first(
        candidates, downloader, str(tmp_path), 7, 1, registry, set(),
        failures=failures,
    )
    assert asset is None
    # Two speculative downloads happened (each demoted as
    # unconfirmed); the third was refused by the per-beat cap
    # BEFORE any request was made.
    assert len(calls) == tier_ladder.SPECULATIVE_PER_BEAT_DOWNLOAD_CAP
    assert [f["reason"] for f in failures] == [
        rd.SPECULATIVE_FRAME_UNCONFIRMED_REASON,
        rd.SPECULATIVE_FRAME_UNCONFIRMED_REASON,
        rd.SPECULATIVE_DOWNLOAD_BUDGET_REASON,
    ]
    ledger = tier_ladder.ledger_for(registry)
    assert ledger.speculative_downloads == (
        tier_ladder.SPECULATIVE_PER_BEAT_DOWNLOAD_CAP
    )

    # Production account spent: a fresh beat on the same registry
    # is refused once escalation + speculative reach 20 in total.
    for _ in range(
        tier_ladder.PRODUCTION_MAX_EXTRA_DOWNLOADS
        - ledger.speculative_downloads
    ):
        ledger.spend_download()
    calls.clear()
    failures.clear()
    asset = pipeline_verification._uve_fetch_first(
        [_fetch_candidate("https://media.example.org/spec-cap-late")],
        downloader, str(tmp_path), 9, 1, registry, set(),
        failures=failures,
    )
    assert asset is None
    assert calls == []  # refused before any request
    assert failures[0]["reason"] == rd.SPECULATIVE_DOWNLOAD_BUDGET_REASON


# ---------------------------------------------------------------------------
# Decision ledger: the marker reaches the rows
# ---------------------------------------------------------------------------

def _record(**over):
    record = {
        "candidate_id": "cand-x",
        "url": "https://media.example.org/x",
        "provider": "wikimedia",
        "kind": "image",
        "outcome": "verified",
        "stage": "verification",
        "reason": "",
        "primary_rejection_reason": "",
        "score": 0.9,
        "score_basis": "verifier_score",
        "contributions": {"subject": 0.45, "topical": 0.45, "era": 0.1},
        "verdict_reasons": [],
        "contributing_reasons": [],
        "dimension_results": None,
        "era_waived": False,
        "title": "Berlin Wall crowds",
        "specificity": "representative",
        "category": "evidence",
        "matched_query": "berlin wall crowds",
        "shot_index": 0,
        "beat_id": "beat-1",
        "sentence_numbers": [7],
        "segment_id": None,
        "tier": "direct",
        "metadata_absence_bypass": True,
        "metadata_absence_penalty": 0.1,
        "absence_reasons": ["missing_subject"],
    }
    record.update(over)
    return record


def test_ledger_rows_carry_the_bypass_marker(monkeypatch):
    from models import AssetDecision

    engine = create_engine("sqlite://")
    AssetDecision.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(run_ledger, "_session", factory)

    report = {
        "candidate_records": {
            "records": [
                _record(candidate_id="cand-verified"),
                _record(
                    candidate_id="cand-frame-rejected",
                    outcome="download_failed",
                    stage="frame_verification",
                    reason=rd.SPECULATIVE_FRAME_UNCONFIRMED_REASON,
                ),
                _record(
                    candidate_id="cand-not-picked",
                    outcome="rejected",
                    stage="selection",
                    reason="pick_limit_reached",
                    primary_rejection_reason="pick_limit_reached",
                ),
            ]
        }
    }
    written = run_ledger.record_decisions(
        "run-bypass-test", "proj-bypass-test", report, {"items": []},
        processed_scenes=[],
    )
    assert written == 3
    session = factory()
    try:
        rows = {
            row.asset_key: row
            for row in session.query(AssetDecision).all()
        }
    finally:
        session.close()
    assert set(rows) == {
        "cand-verified", "cand-frame-rejected", "cand-not-picked",
    }
    for row in rows.values():
        verification = row.verification or {}
        assert verification["metadata_absence_bypass"] is True
        assert verification["metadata_absence_penalty"] == 0.1
        assert verification["absence_reasons"] == ["missing_subject"]
    assert rows["cand-verified"].decision_type == "verified_not_picked"
    assert rows["cand-frame-rejected"].decision_type == "download_failed"
    assert rows["cand-not-picked"].decision_type == "rejected"
