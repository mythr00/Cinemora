"""Phase 3 (Master Production Recovery — Global Visual Planning)
regression tests.

Covered, by directive §11/§14 acceptance mapping:
- the authenticity taxonomy (services.authenticity): classification
  precedence, the unknown-by-default honesty rule, and the
  direct-evidence requirement pinned against
  story_understanding.resolve_visual_category;
- acceptance #10: illustrative stock can never satisfy a
  direct-evidence need (verify_candidate hard rule), while staying
  legitimate for representative/generic needs;
- the evidence ledger (discovery_memory): verified -> accepted
  upgrades, content ownership, duplicate links (#11 at store level);
- acceptance #14: the global assignment planner allocates a scarce
  asset to the unit it serves best, with opportunity cost;
- acceptance #7 (assignment half): a reserved candidate is refused
  by _uve_pick with the recorded global_assignment_reserved reason;
- acceptance #12: segment ranges are unified on the registry —
  committed and planned ranges both block new selections;
- acceptance #9: timeline items, candidate records, the rights
  manifest and the certificate all carry the classification;
- the planning pathway: the dead planners are gone and plan_script
  remains the intent authority;
- scarce-footage honesty: a scene with no discoverable assets
  completes with the gap recorded, never padded.
"""

from pathlib import Path

import pytest

from services import authenticity
from services import discovery_memory
from services import global_assignment
from services import media_pipeline as mp
from services import plan_document
from services import rejection_diagnostics
from services import run_metrics
from services import story_understanding as story
from services.asset_report import build_asset_report, build_rights_manifest
from services.pipeline_assignment import _uve_pick
from services.timeline import build_scene_timeline
from services.visual_registry import VisualRegistry, get_registry, reset_registry


# ------------------------------------------------------- authenticity

def _stock_candidate(**over):
    candidate = {
        "title": "Crowds crossing a checkpoint in 1989",
        "snippet": "crowds crossing checkpoint",
        "url": "https://www.pexels.com/video/12345",
        "provider": "pexels",
        "license": "Pexels License",
        "illustrative_stock": True,
        "provenance": "illustrative_stock",
        "kind": "video",
    }
    candidate.update(over)
    return candidate


def test_classify_precedence_and_unknown_default():
    assert authenticity.classify_candidate(_stock_candidate()) == (
        authenticity.ILLUSTRATIVE_STOCK
    )
    # No provenance signal at all -> unknown, never authentic.
    assert authenticity.classify_candidate({"kind": "image"}) == (
        authenticity.UNKNOWN
    )
    assert authenticity.classify_candidate({}) == authenticity.UNKNOWN
    # Sourced archival material classifies by kind.
    assert authenticity.classify_candidate({
        "kind": "video", "provider": "archive_org",
        "url": "https://archive.org/details/x",
    }) == authenticity.AUTHENTIC_FOOTAGE
    assert authenticity.classify_candidate({
        "kind": "image", "source_url": "https://commons.wikimedia.org/x",
    }) == authenticity.AUTHENTIC_PHOTO
    # The B-roll tier axis: related activity is contextual material.
    assert authenticity.classify_candidate({
        "kind": "video", "provider": "youtube",
        "url": "https://youtube.com/watch?v=x",
        "tier": "activity_broll",
    }) == authenticity.CONTEXTUAL_BROLL
    # Generated graphics and AI imagery are recognised as such.
    assert authenticity.classify_candidate({
        "type": "motion_graphic", "generated": True,
        "source": "cinemora_motion_graphics",
    }) == authenticity.GENERATED_GRAPHIC
    assert authenticity.classify_candidate({
        "kind": "image", "ai_generated": True,
        "url": "https://example.org/x.png",
    }) == authenticity.AI_IMAGERY
    # A valid stamp decided earlier always wins.
    assert authenticity.classify_candidate({
        "kind": "image", "classification": "authentic_photo",
    }) == authenticity.AUTHENTIC_PHOTO


def test_requires_direct_evidence_matches_resolved_category():
    """The taxonomy's direct-evidence test and the verifier's
    resolved category are two spellings of one rule — pin them."""
    shapes = [
        {"specificity": "exact"},
        {"specificity": "representative"},
        {"specificity": "generic"},
        {"specificity": "representative", "role": "evidence"},
        {"specificity": "generic", "shot_type": "document"},
        {"specificity": "representative", "shot_type": "establishing"},
    ]
    for shape in shapes:
        assert authenticity.requires_direct_evidence(shape) == (
            story.resolve_visual_category(shape) == "evidence"
        ), shape


def test_can_satisfy_matrix():
    evidence_need = {"specificity": "exact"}
    open_need = {"specificity": "representative"}
    for cls in authenticity.CLASSIFICATIONS:
        assert authenticity.can_satisfy(cls, open_need) is True
    for cls in (
        authenticity.AUTHENTIC_FOOTAGE,
        authenticity.AUTHENTIC_PHOTO,
        authenticity.AUTHENTIC_DOCUMENT,
    ):
        assert authenticity.can_satisfy(cls, evidence_need) is True
    for cls in (
        authenticity.ILLUSTRATIVE_STOCK,
        authenticity.CONTEXTUAL_BROLL,
        authenticity.GENERATED_GRAPHIC,
        authenticity.AI_IMAGERY,
        authenticity.UNKNOWN,
    ):
        assert authenticity.can_satisfy(cls, evidence_need) is False


def _bible():
    return story.hydrate_bible({
        "entities": [
            {"id": "berlin_wall", "canonical": "Berlin Wall",
             "aliases": ["the Wall"], "type": "object"},
        ]
    })


def test_illustrative_stock_rejected_for_evidence_need():
    """Acceptance #10: an evidence (exact) need refuses illustrative
    stock in the verification chain itself, however relevant."""
    scenario = {
        "subject_id": "berlin_wall",
        "specificity": "exact",
        "search_terms": ["crowds crossing checkpoint"],
        "visual": "crowds of people crossing through an opened checkpoint",
        "era": "1989",
    }
    verdict = story.verify_candidate(
        _stock_candidate(), scenario, _bible()
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == (
        story.ILLUSTRATIVE_NOT_EVIDENCE_REASON
    )
    # The reason is contextual — it must never enter the portable
    # intrinsic index (the same stock photo stays usable elsewhere).
    from services.pipeline_verification import (
        _INTRINSIC_REJECTION_REASONS,
    )
    assert (
        story.ILLUSTRATIVE_NOT_EVIDENCE_REASON
        not in _INTRINSIC_REJECTION_REASONS
    )


def test_illustrative_stock_still_serves_open_need():
    """The same candidate under a representative need is judged on
    relevance, not refused for its provenance."""
    scenario = {
        "subject_id": "berlin_wall",
        "specificity": "representative",
        "search_terms": ["crowds crossing checkpoint"],
        "visual": "crowds of people crossing through an opened checkpoint",
        "era": "1989",
    }
    candidate = _stock_candidate(
        title="Berlin Wall crowds crossing checkpoint 1989",
        snippet="crowds crossing checkpoint at the Berlin Wall",
    )
    verdict = story.verify_candidate(candidate, scenario, _bible())
    assert verdict["rejection_reason"] != (
        story.ILLUSTRATIVE_NOT_EVIDENCE_REASON
    )


def test_verdict_code_version_bumped_for_the_new_rule():
    # Verdicts recorded before the illustrative rule existed may
    # accept what the rule now rejects; the store's version keying
    # must keep them unreachable.
    assert discovery_memory.VERDICT_CODE_VERSION == "2"
    from services.pipeline_verification import _VERDICT_CANDIDATE_FIELDS

    assert "illustrative_stock" in _VERDICT_CANDIDATE_FIELDS
    assert "provenance" in _VERDICT_CANDIDATE_FIELDS


# ---------------------------------------------------- evidence ledger

@pytest.fixture()
def store(tmp_path):
    return discovery_memory.DiscoveryMemory.open(str(tmp_path))


def _entry(**over):
    entry = {
        "candidate_id": "cand:1",
        "sentence_number": 3,
        "url": "https://example.org/a.jpg",
        "provider": "wikimedia",
        "kind": "image",
        "tier": "direct",
        "classification": "authentic_photo",
        "score": 0.9,
        "outcome": "verified",
    }
    entry.update(over)
    return entry


def test_evidence_upgrade_and_ownership(store):
    assert store.record_evidence(_entry()) is True
    assert store.evidence_content_owner("hash-a") is None
    # The commit point upgrades the row: accepted, with the bytes.
    assert store.record_evidence(_entry(
        outcome="accepted", content_hash="hash-a",
    )) is True
    rows = store.evidence_for_sentence(3)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "accepted"
    assert rows[0]["content_hash"] == "hash-a"
    owner = store.evidence_content_owner("hash-a")
    assert owner is not None and owner["sentence_number"] == 3
    # A later weaker re-record never downgrades the row.
    assert store.record_evidence(_entry()) is True
    assert store.evidence_for_sentence(3)[0]["outcome"] == "accepted"
    assert store.evidence_for_sentence(3)[0]["content_hash"] == "hash-a"


def test_evidence_duplicate_rows_do_not_own_content(store):
    """Acceptance #11 at the ledger level: byte-identical content
    has exactly one owner; the duplicate row links to it."""
    store.record_evidence(_entry(outcome="accepted",
                                 content_hash="hash-a"))
    store.record_evidence(_entry(
        candidate_id="cand:2", sentence_number=6,
        url="https://mirror.example.org/a.jpg",
        outcome="accepted", content_hash="hash-a",
    ))
    assert store.mark_evidence_duplicate(
        "cand:2", 6, content_hash="hash-a", duplicate_of="sentence:3",
    ) is True
    owner = store.evidence_content_owner("hash-a")
    assert owner["candidate_id"] == "cand:1"
    dup = store.evidence_for_sentence(6)[0]
    assert dup["duplicate_of"] == "sentence:3"
    assert store.summary()["evidence"] == 2


# ------------------------------------------------- global assignment

def _unit(key, *candidates):
    return {"key": key, "candidates": list(candidates)}


def _cand(identity, source, score):
    return {"identity": identity, "source_key": source, "score": score}


def test_planner_gives_scarce_asset_to_the_unit_that_needs_it():
    """Acceptance #14: the flexible unit runs first in scene order
    and scores the shared asset nearly as high — the constrained
    unit still gets it, and the flexible unit takes its abundant
    alternative and records the reservation."""
    units = [
        _unit("flexible",
              _cand("shared", "src-shared", 0.80),
              _cand("own", "src-own", 0.79)),
        _unit("constrained",
              _cand("shared", "src-shared", 0.95)),
    ]
    plan = global_assignment.plan_assignments(units)
    assert plan["constrained"]["picks"] == ["shared"]
    assert plan["flexible"]["picks"] == ["own"]
    assert plan["flexible"]["reserved"] == {"shared": "constrained"}


def test_planner_opportunity_cost_breaks_near_ties_toward_abundance():
    units = [
        _unit("a", _cand("contested", "s1", 0.90),
              _cand("quiet", "s2", 0.89)),
        _unit("b", _cand("contested", "s1", 0.90)),
        _unit("c", _cand("contested", "s1", 0.90)),
    ]
    plan = global_assignment.plan_assignments(units, limit=1)
    # b and c have no alternative; one of them wins "contested" and
    # unit a — processed with its alternatives in view — must not
    # be the one holding it while another unit starves... a is the
    # MOST flexible, so it is served last and takes "quiet".
    assert plan["a"]["picks"] == ["quiet"]
    winners = [
        key for key in ("b", "c") if plan[key]["picks"] == ["contested"]
    ]
    assert len(winners) == 1


def test_planner_respects_source_cap_and_empty_pools():
    units = [
        _unit("a", _cand("c1", "same", 0.9), _cand("c2", "same", 0.8),
              _cand("c3", "same", 0.7), _cand("c4", "same", 0.6)),
        _unit("empty"),
    ]
    plan = global_assignment.plan_assignments(
        units, limit=3, source_cap=3, source_usage={"same": 1},
    )
    assert len(plan["a"]["picks"]) == 2  # cap 3 - 1 already used
    assert plan["empty"] == {"picks": [], "reserved": {}, "contended": 0}


def test_pick_refuses_reserved_candidate_with_recorded_reason():
    """Acceptance #7 (assignment half): the pick stamps the truthful
    reason instead of consuming a reserved candidate."""
    registry = VisualRegistry(project_id="p3-pick")
    shared = {
        "candidate_id": "cand-shared",
        "url": "https://media.example.org/shared",
        "title": "shared",
        "verification": {"accepted": True, "score": 0.9},
        "universal_score": 0.9,
        "_selection_score": 0.9,
        "_global_reserved_for": ("beat_2", 1),
    }
    own = {
        "candidate_id": "cand-own",
        "url": "https://media.example.org/own",
        "title": "own",
        "verification": {"accepted": True, "score": 0.8},
        "universal_score": 0.8,
        "_selection_score": 0.8,
    }
    picks = _uve_pick([shared, own], {}, registry, set())
    assert [p["candidate_id"] for p in picks] == ["cand-own"]
    assert shared["reject_reason"] == "global_assignment_reserved"
    assert shared["rejection_stage"] == "global_assignment"


# --------------------------------------------------------- segments

def test_registry_planned_and_committed_ranges_unified():
    """Acceptance #12: one store — a selector avoiding ranges sees
    committed and planned ranges together, and the plan survives
    an export/restore round trip."""
    registry = VisualRegistry(project_id="p3-seg")
    url = "https://youtube.com/watch?v=unified"
    registry.register(url=url, title="t", clip_key="k1", start=0, end=8)
    registry.plan_segment(url, 40, 48)
    assert registry.segment_ranges_in_use(url) == [(0.0, 8.0), (40.0, 48.0)]
    assert registry.is_segment_used(url, 4, 12)  # committed branch
    restored = VisualRegistry(project_id="p3-seg")
    restored.restore_state(registry.export_state())
    assert restored.segment_ranges_in_use(url) == [(0.0, 8.0), (40.0, 48.0)]
    restored.clear_planned_segments()
    assert restored.segment_ranges_in_use(url) == [(0.0, 8.0)]


def test_positional_window_avoids_committed_and_planned_ranges():
    url = "https://youtube.com/watch?v=p3-window"
    reset_registry()
    registry = get_registry("")
    try:
        assert mp._positional_direct_window(url, 64, 8) == 0
        # The window above PLANNED (0, 8) on the registry; the next
        # call must skip it without any download committing it.
        assert mp._positional_direct_window(url, 64, 8) == 8
        registry.register(url=url, title="t", clip_key="k", start=16, end=24)
        assert mp._positional_direct_window(url, 64, 8) == 24
    finally:
        reset_registry()


# ---------------------------------------------------------- carriage

def test_build_event_classification_default_is_unknown():
    event = rejection_diagnostics.build_event(
        candidate_id="c", url="https://x", provider="p", kind="image",
        outcome="verified", stage="verification", reason="",
    )
    assert event["classification"] == "unknown"
    event = rejection_diagnostics.build_event(
        candidate_id="c", url="https://x", provider="p", kind="image",
        outcome="verified", stage="verification", reason="",
        classification="illustrative_stock",
    )
    assert event["classification"] == "illustrative_stock"


def _visual(tmp_path, name, **over):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 2048)
    visual = {
        "file_path": str(path), "path": str(path), "type": "video",
        "title": name, "verification": {"accepted": True},
    }
    visual.update(over)
    return visual


def _timings(n, step=5.0):
    return [
        {"sentence_index": i, "text": f"Sentence {i}.",
         "start": float(i * step), "end": float(i * step + 4)}
        for i in range(n)
    ]


def test_timeline_items_carry_classification(tmp_path):
    """Acceptance #9: every timeline item carries a valid
    classification; unstamped provenance-less visuals are unknown,
    never authentic by default (the tier default is untouched)."""
    bare = _visual(tmp_path, "bare.mp4")
    stock = _visual(
        tmp_path, "stock.mp4", tier="activity_broll",
        classification="illustrative_stock",
    )
    graphic = _visual(
        tmp_path, "graphic.mp4", motion_graphic=True, sentence_index=2,
        validation={"passed": True}, delivered=True,
    )
    scene_media = {
        "scene_number": 1,
        "duration_seconds": 15,
        "images": [],
        "videos": [bare, stock, graphic],
        "sentences": [
            {"videos": [bare], "images": []},
            {"videos": [stock], "images": []},
            {"videos": [], "images": []},
        ],
    }
    result = build_scene_timeline(scene_media, _timings(3))
    by_sentence = {
        item["sentence_index"]: item for item in result["items"]
    }
    assert by_sentence[0]["classification"] == "unknown"
    assert by_sentence[0]["tier"] == "direct"  # legacy tier default
    assert by_sentence[1]["classification"] == "illustrative_stock"
    assert by_sentence[2]["classification"] == "generated_graphic"
    for item in result["items"]:
        assert authenticity.is_valid_classification(
            item["classification"]
        )


def test_asset_report_classification_census_and_ledger():
    scenes = [{
        "scene_number": 1,
        "videos": [],
        "images": [{
            "file_path": "/x/stock.jpg", "path": "/x/stock.jpg",
            "type": "image", "title": "stock",
            "provider": "pexels", "license": "Pexels License",
            "illustrative_stock": True,
            "provenance": "illustrative_stock",
            "verification": {"accepted": True},
            "sentence_numbers": [1],
        }],
        "sentences": [{"sentence_number": 1, "videos": [], "images": []}],
    }]
    timeline = {"items": [
        {"assignment": "sentence", "scene_number": 1,
         "sentence_index": 0, "file_path": "/x/stock.jpg",
         "duration": 5.0, "classification": "illustrative_stock"},
        {"assignment": "sentence", "scene_number": 1,
         "sentence_index": 1, "file_path": "/x/legacy.jpg",
         "duration": 5.0},
    ]}
    ledger = [
        {"candidate_id": "cand:1", "sentence_number": 1,
         "outcome": "accepted", "content_hash": "hash-a",
         "duplicate_of": None},
        {"candidate_id": "cand:2", "sentence_number": 2,
         "outcome": "accepted", "content_hash": "hash-a",
         "duplicate_of": "sentence:1"},
        {"candidate_id": "cand:3", "sentence_number": 2,
         "outcome": "verified", "content_hash": None,
         "duplicate_of": None},
    ]
    report = build_asset_report(
        scenes, timeline_result=timeline, evidence_ledger=ledger,
    )
    census = report["authenticity"]
    assert census["illustrative_stock_assets"] == 1
    assert census["classification_counts"]["illustrative_stock"] == 1
    assert census["timeline_items_without_classification"] == 1
    section = report["evidence_ledger"]
    assert section["state"] == "recorded"
    assert section["accepted_assets"] == 2
    assert section["distinct_content_hashes"] == 1  # #11: one content
    assert section["duplicate_rows"] == 1
    manifest = build_rights_manifest(scenes)
    assert manifest[0]["classification"] == "illustrative_stock"


def test_certificate_rows_carry_classification():
    decisions = [{
        "sentence_number": 1, "scene_number": 1,
        "decision_type": "selected",
        "verification": {
            "accepted": True, "tier": "direct",
            "classification": "authentic_photo",
        },
        "rights": {"license": "CC BY"},
    }, {
        "sentence_number": 2, "scene_number": 1,
        "decision_type": "selected",
        "verification": {"accepted": True},
        "rights": {},
    }]
    certificate = plan_document.build_certificate(
        {"id": "p", "title": "t", "category": "c"},
        {"id": "r"}, decisions, {},
    )
    rows = certificate["assets"] if "assets" in certificate else (
        certificate.get("rows") or []
    )
    assert rows[0]["classification"] == "authentic_photo"
    assert rows[1]["classification"] is None  # unstated, never invented


# --------------------------------------------------- planning pathway

def test_dead_planners_removed_plan_script_is_authority():
    import importlib.util

    assert importlib.util.find_spec("services.visual_plan") is None
    assert not hasattr(mp, "build_visual_plan")
    assert callable(story.plan_script)


# ------------------------------------------- scene-level integration

def _need(beat_id, queries):
    return {
        "beat_id": beat_id, "shot_index": 1,
        "shot_type": "place", "specificity": "generic",
        "visual_category": "contextual", "description": "",
        "required_subject_id": None, "required_subject": "",
        "required_type": "place", "era": "", "place": "",
        "event": "", "visual_requirement": "", "forbidden": [],
        "priority": 2, "tier": "direct",
        "fallback_concepts": {}, "concepts_source": "test",
        "visual_intents": None, "queries": queries,
    }


def test_scene_global_assignment_end_to_end(monkeypatch, tmp_path):
    """Acceptance #14 at pipeline level: two beats contend for one
    verified candidate; the global plan gives it to the beat with
    no alternative, and the scene stats + candidate records say so."""
    import uuid

    project_id = f"p3-global-{uuid.uuid4()}"
    sentences = [
        {"sentence_number": 1, "text": "The flexible beat."},
        {"sentence_number": 2, "text": "The constrained beat."},
    ]
    beats = [
        {"beat_id": "beat_flex", "subject_id": None, "place": None,
         "searchable": True, "role": "event",
         "sentence_numbers": [1], "sentences": [sentences[0]],
         "text": sentences[0]["text"],
         "shot_needs": [_need("beat_flex", ["flexible scene"])]},
        {"beat_id": "beat_constrained", "subject_id": None,
         "place": None, "searchable": True, "role": "event",
         "sentence_numbers": [2], "sentences": [sentences[1]],
         "text": sentences[1]["text"],
         "shot_needs": [_need("beat_constrained", ["constrained scene"])]},
    ]
    scenarios = {
        "beat_flex": [{
            "sentence_number": 1, "specificity": "representative",
            "search_terms": ["flexible"], "visual": "flexible scene",
            "queries": ["flexible scene"],
        }],
        "beat_constrained": [{
            "sentence_number": 2, "specificity": "representative",
            "search_terms": ["constrained"], "visual": "constrained scene",
            "queries": ["constrained scene"],
        }],
    }
    shared = {
        "url": "https://media.example.org/shared",
        "title": "shared asset", "provider": "youtube", "kind": "video",
    }
    own = {
        "url": "https://media.example.org/own",
        "title": "own asset", "provider": "youtube", "kind": "video",
    }
    scores = {shared["url"]: 0.90, own["url"]: 0.85}

    def fake_search(scenario_, cache, pid, shot_need=None):
        if scenario_["visual"] == "flexible scene":
            return ([dict(shared), dict(own)], [], [])
        return ([dict(shared)], [], [])

    def fake_verify(candidates, scenario_, bible_, shot_need=None):
        scored = []
        for cand in candidates:
            score = scores[cand["url"]]
            item = dict(cand)
            item["candidate_id"] = f"cand:{cand['url'].rsplit('/', 1)[-1]}"
            item["verification"] = {"accepted": True, "score": score}
            item["universal_score"] = score
            item["relevance_score"] = score
            scored.append(item)
        return scored

    downloads = {}

    def download(candidate, scene_dir, sentence_number, asset_index):
        downloads[sentence_number] = candidate["url"]
        path = Path(scene_dir) / f"asset-{sentence_number}.mp4"
        path.write_bytes(b"fake")
        asset = dict(candidate)
        asset.update({
            "file_path": str(path), "path": str(path),
            "type": "video", "visual_type": "video",
            "segment_id": f"seg_{sentence_number}",
            "duration_seconds": 5.0,
        })
        return asset

    monkeypatch.setattr(mp, "build_story_beats", lambda s, b: beats)
    monkeypatch.setattr(mp, "_load_case_bible", lambda s, p: {})
    monkeypatch.setattr(
        mp, "_beat_scenarios", lambda b: scenarios[b["beat_id"]]
    )
    monkeypatch.setattr(mp, "_uve_scenario_search", fake_search)
    monkeypatch.setattr(mp, "_uve_verify_all", fake_verify)
    monkeypatch.setattr(mp, "_uve_download_any_candidate", download)

    scene = {
        "scene_number": 1, "project_id": project_id,
        "sentences": sentences,
    }
    run_metrics.begin_run(project_id)
    result = mp._uve_process_scene_media(scene, str(tmp_path))

    # The constrained beat (sentence 2) got the shared asset; the
    # flexible beat took its own alternative.
    assert downloads[2] == shared["url"]
    assert downloads[1] == own["url"]
    stats = result["asset_stats"]
    assert stats["global_assignment"]["reservations"] == 1
    assert stats["global_assignment"]["planned_picks"] == 2
    reserved_records = [
        record for record in stats.get("candidate_records") or []
        if record.get("reason") == "global_assignment_reserved"
    ]
    assert len(reserved_records) == 1
    assert reserved_records[0]["candidate_id"] == "cand:shared"


def test_scene_with_no_discoverable_assets_completes_honestly(
    monkeypatch, tmp_path,
):
    """Scarce footage: nothing verifies anywhere — the scene
    completes, the gap is recorded, and no asset is fabricated."""
    import uuid

    project_id = f"p3-scarce-{uuid.uuid4()}"
    sentences = [{"sentence_number": 1, "text": "An unfilmable moment."}]
    beats = [{
        "beat_id": "beat_scarce", "subject_id": None, "place": None,
        "searchable": True, "role": "event",
        "sentence_numbers": [1], "sentences": sentences,
        "text": sentences[0]["text"],
        "shot_needs": [_need("beat_scarce", ["unfilmable moment"])],
    }]
    scenarios = {
        "beat_scarce": [{
            "sentence_number": 1, "specificity": "representative",
            "search_terms": ["unfilmable"], "visual": "unfilmable moment",
            "queries": ["unfilmable moment"],
        }],
    }
    monkeypatch.setattr(mp, "build_story_beats", lambda s, b: beats)
    monkeypatch.setattr(mp, "_load_case_bible", lambda s, p: {})
    monkeypatch.setattr(
        mp, "_beat_scenarios", lambda b: scenarios[b["beat_id"]]
    )
    monkeypatch.setattr(
        mp, "_uve_scenario_search",
        lambda scenario_, cache, pid, shot_need=None: ([], [], []),
    )
    scene = {
        "scene_number": 1, "project_id": project_id,
        "sentences": sentences,
    }
    run_metrics.begin_run(project_id)
    result = mp._uve_process_scene_media(scene, str(tmp_path))
    assert result["videos"] == [] and result["images"] == []
    stats = result["asset_stats"]
    assert stats["global_assignment"]["units_with_pools"] == 0
    assert stats["global_assignment"]["planned_picks"] == 0
    assert stats.get("missing_visuals")
