"""Intelligent B-Roll B0 — tier stamping + reporting (plan §6.1, §11 B0).

B0 threads the closed tier vocabulary (direct / related /
activity_broll / atmospheric / graphic) through today's pipeline —
candidate events -> accepted-asset records -> timeline items ->
asset report (tier_breakdown) -> visual quality (tier_accounting)
-> production report (q26) -> ledger verification JSON ->
certificate — with ZERO behaviour change: no search, pick,
verification or timeline decision changes, and assignments are
pinned unchanged below. Every search the pipeline runs today is a
direct (T1) search, so today's stamps are "direct" (graphics
"graphic").

B0 also names the sentences B0 must not lose: sentences in
unsearched-role beats are recorded in scene_stats
["uncovered_unsearched"] and surfaced in the asset report —
deliberately NOT in missing_visuals, so graphics targeting is
unchanged until B2.
"""

import uuid
from pathlib import Path

import pytest

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import media_pipeline as mp
from services import plan_document
from services import rejection_diagnostics as rd
from services import run_ledger
from services.asset_report import build_asset_report
from services.production_report import build_production_report
from services.timeline import build_scene_timeline
from services.visual_quality import measure_visual_quality
from services.visual_registry import VisualRegistry


# ---------------------------------------------------------------------------
# Candidate events
# ---------------------------------------------------------------------------

def test_build_event_carries_tier_default_direct():
    event = rd.build_event(
        candidate_id="c1", outcome="rejected", stage="scoring",
        reason="below_threshold",
    )
    assert event["tier"] == "direct"


def test_build_event_explicit_tier_and_normalisation():
    event = rd.build_event(
        candidate_id="c2", outcome="accepted", stage="accepted",
        tier="activity_broll",
    )
    assert event["tier"] == "activity_broll"
    # A value outside the closed vocabulary never survives as-is.
    bogus = rd.build_event(
        candidate_id="c3", outcome="accepted", stage="accepted",
        tier="stock_footage",
    )
    assert bogus["tier"] == "direct"


# ---------------------------------------------------------------------------
# Shot needs: tier replaces the fossil fallback field
# ---------------------------------------------------------------------------

def _sentence(number, text, *, role="action", scenarios=None):
    return {
        "sentence_number": number,
        "text": text,
        "role": role,
        "subject_id": None,
        "scenarios": scenarios if scenarios is not None else [{
            "visual": "archival material",
            "kind": "archival",
            "subject_id": None,
            "specificity": "representative",
            "search_terms": [],
            "queries": ["archival material"],
            "place": None,
            "era": None,
        }],
    }


def test_shot_need_tier_replaces_fossil_fallback():
    beats = mp.build_story_beats(
        [_sentence(1, "Police patrolled the highway all night.")], {}
    )
    assert len(beats) == 1
    need = beats[0]["shot_needs"][0]
    assert need["tier"] == "direct"
    # §3.4: the write-only fossil must not survive beside the tier.
    assert "fallback" not in need


# ---------------------------------------------------------------------------
# Pass level: accepted assets + candidate records carry the tier
# ---------------------------------------------------------------------------

def _verified_video(index, host):
    return {
        "url": f"https://{host}/berlin-{index}",
        "title": f"Berlin Wall crowds {index}",
        "snippet": "berlin wall crowds",
        "provider": "youtube",
    }


def test_pass_stamps_tier_on_asset_and_records(monkeypatch, tmp_path):
    videos = [_verified_video(0, "b0.example.org")]
    monkeypatch.setattr(
        mp, "_uve_scenario_search",
        lambda scenario, cache, pid, need: (list(videos), [], []),
    )

    def download(candidate, scene_dir, sentence_number, asset_index):
        path = Path(scene_dir) / "asset_0.mp4"
        path.write_bytes(b"fake")
        asset = dict(candidate)
        asset.update({
            "file_path": str(path), "path": str(path),
            "type": "video", "visual_type": "video",
            "duration_seconds": 5.0,
        })
        return asset

    monkeypatch.setattr(mp, "_uve_download_any_candidate", download)
    scene_stats = {
        "candidates_considered": 0, "candidates_rejected": 0,
        "downloads_failed": 0, "providers": {}, "kinds": {},
    }
    sentence = {
        "sentence_number": 1, "text": "The Berlin Wall fell in 1989.",
        "role": "action", "subject_id": None,
        "scenarios": [{
            "visual": "berlin wall crowds", "kind": "archival",
            "subject_id": None, "subject": "Berlin Wall",
            "specificity": "representative",
            "search_terms": ["berlin wall"], "queries": ["berlin wall"],
            "place": None, "era": None,
        }],
    }
    beat = mp.build_story_beats([sentence], {})[0]
    shot_need = beat["shot_needs"][0]
    assert shot_need["tier"] == "direct"
    scenario = mp._beat_scenarios(beat)[0]
    beat_videos, beat_images = [], []
    asset = mp._uve_search_and_pick(
        scenario, shot_need,
        search_cache={}, project_id="t", bible={},
        registry=VisualRegistry(), used_keys=set(),
        scene_dir=tmp_path, scene_stats=scene_stats,
        beat=beat,
        beat_videos=beat_videos, beat_images=beat_images,
        scenario_index=1, queries=["berlin wall"], review_records=[],
    )
    assert asset is not None
    assert asset["tier"] == "direct"
    assert scene_stats["candidate_records"]
    for record in scene_stats["candidate_records"]:
        assert record["tier"] == "direct"


# ---------------------------------------------------------------------------
# Gilgo shape: an unsearched opening sentence is NAMED, not lost
# ---------------------------------------------------------------------------

def test_unsearched_role_sentences_named_not_in_missing(monkeypatch, tmp_path):
    hook_sentence = {
        "sentence_number": 1,
        "text": ("In December 2010, a police officer searching for a "
                 "missing woman near Gilgo Beach, Long Island, made a "
                 "horrifying discovery."),
        "role": "hook",
        "scenarios": [],
    }
    action_sentence = {
        "sentence_number": 2,
        "text": ("For months, investigators studied his phones, his "
                 "movements, and his family travel records."),
        "role": "action",
        "scenarios": [{
            "visual": "analysts reviewing phone records",
            "kind": "person_activity", "specificity": "representative",
            "subject_id": None, "search_terms": ["phone records"],
            "queries": ["analysts reviewing phone records"],
            "place": None, "era": None,
        }],
    }
    sentences = [hook_sentence, action_sentence]
    beats = [
        {
            "beat_id": "beat_01_hook_none_none", "subject_id": None,
            "place": None, "searchable": False, "role": "hook",
            "sentence_numbers": [1], "sentences": [hook_sentence],
            "text": hook_sentence["text"], "shot_needs": [],
        },
        {
            "beat_id": "beat_02_action_none_none", "subject_id": None,
            "place": None, "searchable": True, "role": "action",
            "sentence_numbers": [2], "sentences": [action_sentence],
            "text": action_sentence["text"],
            "shot_needs": [{
                "beat_id": "beat_02_action_none_none", "shot_index": 1,
                "shot_type": "person_activity",
                "specificity": "representative",
                "visual_category": "contextual", "description": "",
                "required_subject_id": None, "required_subject": "",
                "required_type": "person", "era": "", "place": "",
                "event": "", "visual_requirement": "",
                "forbidden": [], "priority": 2, "tier": "direct",
                "queries": ["analysts reviewing phone records"],
            }],
        },
    ]
    monkeypatch.setattr(mp, "build_story_beats", lambda s, b: beats)
    monkeypatch.setattr(mp, "_load_case_bible", lambda s, p: {})
    monkeypatch.setattr(
        mp, "_uve_scenario_search",
        lambda scenario, cache, pid, need: ([], [], []),
    )
    scene = {
        "scene_number": 1,
        "project_id": f"b0-gilgo-{uuid.uuid4()}",
        "sentences": sentences,
    }
    result = mp._uve_process_scene_media(scene, str(tmp_path))
    stats = result["asset_stats"]

    # The hook sentence is named in the new field, with its beat/role.
    assert stats["uncovered_unsearched"] == [
        {"sentence_number": 1, "beat_id": "beat_01_hook_none_none",
         "role": "hook"}
    ]
    # The searched-and-starved sentence is in missing_visuals; the
    # hook sentence is NOT (B0 must not change graphics targeting).
    missing_numbers = {
        entry["sentence_number"] for entry in stats["missing_visuals"]
    }
    assert missing_numbers == {2}

    # And the asset report surfaces the unsearched sentence by name.
    report = build_asset_report([result], timeline_result=None)
    assert report["uncovered_unsearched_count"] == 1
    assert report["uncovered_unsearched"][0]["sentence_number"] == 1
    assert report["uncovered_unsearched"][0]["role"] == "hook"


# ---------------------------------------------------------------------------
# Timeline items
# ---------------------------------------------------------------------------

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


def test_timeline_items_carry_tier(tmp_path):
    direct = _visual(tmp_path, "direct.mp4")
    broll = _visual(tmp_path, "broll.mp4", tier="activity_broll")
    graphic = _visual(
        tmp_path, "graphic.mp4", motion_graphic=True, sentence_index=2,
    )
    scene_media = {
        "scene_number": 1,
        "duration_seconds": 15,
        "images": [],
        "videos": [direct, broll, graphic],
        "sentences": [
            {"videos": [direct], "images": []},
            {"videos": [broll], "images": []},
            {"videos": [], "images": []},
        ],
    }
    result = build_scene_timeline(scene_media, _timings(3))
    items = result["items"]
    assert len(items) == 3
    by_sentence = {item["sentence_index"]: item for item in items}
    # Behaviour unchanged: the same assignments as before B0.
    assert by_sentence[0]["assignment"] == "sentence"
    assert by_sentence[1]["assignment"] == "sentence"
    assert by_sentence[2]["assignment"] == "motion_graphic"
    # Tiers: unstamped asset -> direct; stamped asset -> its tier;
    # a generated graphic -> graphic.
    assert by_sentence[0]["tier"] == "direct"
    assert by_sentence[1]["tier"] == "activity_broll"
    assert by_sentence[2]["tier"] == "graphic"


def test_timeline_rotation_inherits_asset_tier(tmp_path):
    only = _visual(tmp_path, "only.mp4", tier="related")
    scene_media = {
        "scene_number": 1,
        "duration_seconds": 15,
        "images": [],
        "videos": [only],
        "sentences": [
            {"videos": [only], "images": []},
            {"videos": [], "images": []},
            {"videos": [], "images": []},
        ],
    }
    result = build_scene_timeline(scene_media, _timings(3))
    tiers = [item["tier"] for item in result["items"]]
    assignments = [item["assignment"] for item in result["items"]]
    assert assignments[0] == "sentence"
    assert all(a == "rotation" for a in assignments[1:])
    # Rotation items inherit the tier of the asset they reuse.
    assert tiers == ["related", "related", "related"]


# ---------------------------------------------------------------------------
# Reports: tier_breakdown, tier_accounting, q26
# ---------------------------------------------------------------------------

def _stamped_timeline():
    return {
        "items": [
            {"assignment": "sentence", "scene_number": 1,
             "sentence_index": 0, "file_path": "/x/a.mp4",
             "duration": 10.0, "tier": "direct"},
            {"assignment": "sentence", "scene_number": 1,
             "sentence_index": 1, "file_path": "/x/b.mp4",
             "duration": 6.0, "tier": "activity_broll"},
            {"assignment": "rotation", "scene_number": 1,
             "sentence_index": 2, "file_path": "/x/c.mp4",
             "duration": 4.0, "tier": "atmospheric"},
        ],
        "duration_seconds": 20.0,
        "sentence_count": 3,
    }


def test_asset_report_tier_breakdown_sums_to_timeline():
    report = build_asset_report([], timeline_result=_stamped_timeline())
    breakdown = report["tier_breakdown"]
    assert breakdown["state"] == "recorded"
    total_items = sum(
        entry["items"] for entry in breakdown["by_tier"].values()
    )
    assert total_items == report["timeline_items"] == 3
    total_runtime = sum(
        entry["runtime_seconds"] for entry in breakdown["by_tier"].values()
    )
    assert total_runtime == pytest.approx(20.0)
    # B-roll share = (6 + 4) / 20.
    assert breakdown["broll_share"] == pytest.approx(0.5)
    assert breakdown["unstamped_items"] == 0
    assert len(breakdown["per_sentence"]) == 3


def test_asset_report_tier_breakdown_legacy_not_recorded():
    legacy = {
        "items": [
            {"assignment": "sentence", "scene_number": 1,
             "sentence_index": 0, "file_path": "/x/a.mp4",
             "duration": 10.0},
        ],
    }
    report = build_asset_report([], timeline_result=legacy)
    breakdown = report["tier_breakdown"]
    assert breakdown["state"] == "not_recorded"
    assert breakdown["broll_share"] is None  # unknown, never zero
    assert breakdown["unstamped_items"] == 1


def test_visual_quality_tier_accounting():
    timeline = _stamped_timeline()
    measurement = measure_visual_quality(
        timeline=timeline, asset_report={}, rights_manifest=[],
    )
    accounting = measurement["tier_accounting"]
    assert accounting["state"] == "recorded"
    assert accounting["by_tier"]["direct"]["count"] == 1
    assert accounting["broll_share"] == pytest.approx(0.5)

    legacy = {"items": [dict(_stamped_timeline()["items"][0])]}
    legacy["items"][0].pop("tier")
    legacy_measurement = measure_visual_quality(
        timeline=legacy, asset_report={}, rights_manifest=[],
    )
    assert legacy_measurement["tier_accounting"]["state"] == "not_recorded"
    assert legacy_measurement["tier_accounting"]["broll_share"] is None


def test_production_report_q26_answers_tier_question():
    timeline = _stamped_timeline()
    asset_report = build_asset_report([], timeline_result=timeline)
    measurement = measure_visual_quality(
        timeline=timeline, asset_report=asset_report, rights_manifest=[],
    )
    report = build_production_report(
        measurement=measurement, asset_report=asset_report,
        timeline=timeline, rights_manifest=[],
    )
    q26 = report["questions"]["q26"]
    assert q26["state"] == "answered"
    assert q26["answer"]["direct"] == pytest.approx(0.5)
    assert q26["answer"]["activity_broll"] == pytest.approx(0.3)
    assert q26["answer"]["atmospheric"] == pytest.approx(0.2)
    assert q26["answer"]["broll_share"] == pytest.approx(0.5)
    # The original 25 questions are untouched.
    for i in range(1, 26):
        assert f"q{i:02d}" in report["questions"]

    bare = build_production_report(
        measurement={}, asset_report={}, timeline={}, rights_manifest=[],
    )
    assert bare["questions"]["q26"]["state"] == "unknown"
    assert bare["questions"]["q26"]["answer"] is None


# ---------------------------------------------------------------------------
# Ledger + certificate
# ---------------------------------------------------------------------------

def test_verification_with_segment_stamps_tier():
    asset = {
        "verification": {"accepted": True, "score": 0.9},
        "segment_id": "seg_1",
        "tier": "direct",
    }
    verification = run_ledger._verification_with_segment(asset)
    assert verification["tier"] == "direct"
    assert verification["segment_id"] == "seg_1"
    # The asset's own verdict dict is never mutated.
    assert "tier" not in asset["verification"]

    plain = run_ledger._verification_with_segment(
        {"verification": {"accepted": True}}
    )
    assert "tier" not in plain


def test_certificate_rows_carry_tier():
    decisions = [
        {
            "sentence_number": 1, "scene_number": 1,
            "decision_type": "selected", "asset_key": "a",
            "content_hash": None, "provider": "wikimedia",
            "source_url": "https://example.org/a",
            "interval_start": None, "interval_duration": None,
            "evidence_class": None,
            "verification": {"accepted": True, "tier": "direct"},
            "rights": {"license": "CC0"},
            "previous_decision_id": None, "created_at": None,
        },
        {
            "sentence_number": 2, "scene_number": 1,
            "decision_type": "selected", "asset_key": "b",
            "content_hash": None, "provider": "wikimedia",
            "source_url": "https://example.org/b",
            "interval_start": None, "interval_duration": None,
            "evidence_class": None,
            "verification": {"accepted": True},
            "rights": {"license": "unknown"},
            "previous_decision_id": None, "created_at": None,
        },
    ]
    certificate = plan_document.build_certificate(
        {"id": "p", "title": "t", "category": "Documentary"},
        None, decisions, {},
    )
    assert certificate["assets"][0]["tier"] == "direct"
    # Pre-B0 rows stay honestly unstated.
    assert certificate["assets"][1]["tier"] is None


# --- ledger DB: rejected rows carry the record's tier ----------------------

from database import SessionLocal, engine  # noqa: E402
from models import AssetDecision, Base, ProductionRun, Project  # noqa: E402


@pytest.fixture(autouse=True)
def _ledger_tables():
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Project.__table__,
            ProductionRun.__table__,
            AssetDecision.__table__,
        ],
    )
    yield


@pytest.fixture()
def ledger_project_id():
    pid = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(
            Project(
                id=pid, script="B0 ledger script.",
                category="Documentary", status="created",
            )
        )
        db.commit()
    finally:
        db.close()
    yield pid
    db = SessionLocal()
    try:
        db.query(AssetDecision).filter(
            AssetDecision.project_id == pid
        ).delete()
        db.query(ProductionRun).filter(
            ProductionRun.project_id == pid
        ).delete()
        db.query(Project).filter(Project.id == pid).delete()
        db.commit()
    finally:
        db.close()


def test_ledger_rejected_rows_carry_record_tier(ledger_project_id):
    run_id = run_ledger.begin_run(ledger_project_id, "B0 ledger script.")
    assert run_id
    report = {
        "candidate_records": {
            "records": [
                {
                    "candidate_id": "cand-rej-tier",
                    "url": "https://example.org/rejected",
                    "provider": "youtube",
                    "outcome": "rejected",
                    "stage": "scoring",
                    "reason": "below_threshold",
                    "tier": "direct",
                }
            ]
        }
    }
    written = run_ledger.record_decisions(
        run_id, ledger_project_id, report, {"items": []},
        processed_scenes=[],
    )
    assert written and written >= 1
    rows, _total = run_ledger.list_decisions(run_id)
    rejected = [r for r in rows if r.get("decision_type") == "rejected"]
    assert len(rejected) == 1
    assert rejected[0]["verification"]["tier"] == "direct"
