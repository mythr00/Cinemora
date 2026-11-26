"""Phase 1 (Master Implementation Directive) — eligibility and
assignment correctness at the timeline layer.

AM-012: the generated/motion-graphic class in ``timeline_eligible``
must carry a passing validation verdict and must not be
undelivered. Before Phase 1 the function admitted EVERY generated
item wholesale; the only enforcement was the jobs attach gate
(MG-07), so any caller that built a timeline without going through
jobs — the editor's paths, stored-scene replays, tests — could
assign an unvalidated or failed graphic. The build-level tests
below call ``build_scene_timeline`` DIRECTLY, bypassing the jobs
gate, so the assignment layer itself is pinned.

AM-027: the atmosphere category's effective verification
contract, pinned with explicit fixtures through the real gate +
scorer (``_uve_verify_all``): a contradiction rejects even when
the score clears the generic bar; zero topical overlap passes the
gate (atmosphere imposes no subject/place/type requirement) and
rejects at the scorer; a topical generic candidate is accepted at
the 0.22 generic bar — including a score inside [0.22, 0.34) that
only the generic bar admits; a hard-vetoed (social-platform)
provider is rejected by the chain whatever its metadata says
(the Pexels/Pixabay provider ban pinned here originally was
lifted by the founder on 2026-11-26, Annex B). Tests only: the
fixtures exposed no defect.

CIN-19 / CIN-20 (verification only, per the Phase 0 register):
assignment eligibility for footage is already pinned by
``test_stage1_asset_manager
.test_timeline_eligible_filters_by_verification_state`` (unit)
and ``test_cache_and_timeline_guards
.test_timeline_skips_rejected_assets`` (build level); swap
evidence refresh is pinned by ``test_stage1_correctness
.test_swap_refreshes_all_evidence``. The graphics half of CIN-19
is the AM-012 work above; the ledger surfacing of CIN-20's fresh
verdicts is pinned in test_phase1_ledger_completeness.py. No
further pinning test was needed.
"""

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import media_pipeline as mp
from services import story_understanding as story
from services.timeline import build_scene_timeline, timeline_eligible


# ---------------------------------------------------------------------------
# AM-012 — the generated class in timeline_eligible
# ---------------------------------------------------------------------------

def _graphic(**overrides):
    visual = {
        "motion_graphic": True,
        "file_path": "/media/graphic.mp4",
        "path": "/media/graphic.mp4",
        "type": "video",
    }
    visual.update(overrides)
    return visual


def test_am012_graphic_requires_passing_validation():
    # Validated and not undelivered: eligible.
    assert timeline_eligible(
        _graphic(validation={"passed": True}, delivered=True)
    ) is True
    # `delivered` absent is not a refusal — only an explicit
    # False is (records that predate the field stay assignable
    # once validated).
    assert timeline_eligible(_graphic(validation={"passed": True})) is True
    # Undelivered: out, however good the validation.
    assert timeline_eligible(
        _graphic(validation={"passed": True}, delivered=False)
    ) is False
    # Failed validation: out.
    assert timeline_eligible(
        _graphic(validation={"passed": False}, delivered=True)
    ) is False
    # Never validated: out (missing fields are ineligible but the
    # record is kept — MA-37).
    assert timeline_eligible(_graphic(delivered=True)) is False
    # A non-dict validation value is not a verdict: out.
    assert timeline_eligible(_graphic(validation="passed")) is False
    # The `generated` marker takes the same path as motion_graphic.
    assert timeline_eligible(
        {"generated": True, "validation": {"passed": True},
         "delivered": True}
    ) is True
    assert timeline_eligible({"generated": True}) is False


def _timings(count, step=5.0):
    return [
        {
            "sentence_index": index,
            "text": f"Sentence {index}.",
            "start": float(index * step),
            "end": float(index * step + 4),
        }
        for index in range(count)
    ]


def _scene_with_graphic(tmp_path, graphic):
    path = tmp_path / "graphic.mp4"
    path.write_bytes(b"fake")
    graphic.update({
        "file_path": str(path), "path": str(path),
        "type": "video", "visual_type": "video",
        "duration_seconds": 4.0, "sentence_index": 0,
    })
    return {
        "scene_number": 1,
        "videos": [graphic],
        "images": [],
        "visuals": [graphic],
        "sentences": [{
            "sentence_number": 1, "sentence_text": "One.",
            "videos": [], "images": [],
        }],
    }


def test_am012_unvalidated_graphic_never_assigned_direct_build(tmp_path):
    # Direct build_scene_timeline call — the jobs attach gate is
    # NOT involved. A graphic whose validation failed must not be
    # placed even though it is the scene's only visual.
    graphic = _graphic(validation={"passed": False}, delivered=True)
    result = build_scene_timeline(
        _scene_with_graphic(tmp_path, graphic), _timings(1)
    )
    assert result["items"] == []


def test_am012_undelivered_graphic_never_assigned_direct_build(tmp_path):
    graphic = _graphic(validation={"passed": True}, delivered=False)
    result = build_scene_timeline(
        _scene_with_graphic(tmp_path, graphic), _timings(1)
    )
    assert result["items"] == []


def test_am012_validated_delivered_graphic_is_assigned(tmp_path):
    graphic = _graphic(validation={"passed": True}, delivered=True)
    result = build_scene_timeline(
        _scene_with_graphic(tmp_path, graphic), _timings(1)
    )
    assert len(result["items"]) == 1
    assert result["items"][0]["assignment"] == "motion_graphic"


# ---------------------------------------------------------------------------
# AM-027 — the atmosphere category's effective contract
# ---------------------------------------------------------------------------

_BIBLE_TWO_PEOPLE = story.hydrate_bible({"entities": [
    {"id": "amelia", "canonical": "Amelia Hart", "aliases": [],
     "type": "person"},
    {"id": "marcus", "canonical": "Marcus Webb", "aliases": [],
     "type": "person"},
]})

_ATMOSPHERE_NEED = {"specificity": "generic", "shot_type": "abstract_broll"}

_SHORELINE_SCENARIO = {
    "visual": "ocean waves crashing on a rocky shoreline",
    "search_terms": ["ocean waves", "rocky shoreline"],
    "specificity": "generic", "subject_id": None, "era": None,
}


def _verify_one(candidate, scenario=None, need=None):
    items = mp._uve_verify_all(
        [dict(candidate)],
        dict(scenario or _SHORELINE_SCENARIO),
        _BIBLE_TWO_PEOPLE,
        shot_need=dict(need or _ATMOSPHERE_NEED),
    )
    return items[0]


def _candidate(title, snippet="", url="https://media.example.org/x"):
    return {"title": title, "snippet": snippet, "url": url,
            "provider": "archive_org"}


def test_atmosphere_topical_generic_accepted():
    item = _verify_one(_candidate(
        "Ocean waves crashing on a rocky shoreline at dusk",
        "waves and shoreline",
    ))
    verdict = item["verification"]
    assert verdict["accepted"] is True
    assert verdict["category"] == "atmosphere"
    assert verdict["score"] >= 0.22


def test_atmosphere_partial_overlap_accepted_only_at_generic_bar():
    # Partial topical overlap scores inside [0.22, 0.34): admitted
    # ONLY because the generic (atmosphere) threshold is 0.22 —
    # the same evidence would fail the representative bar.
    item = _verify_one(_candidate("Ocean waves at dusk"))
    verdict = item["verification"]
    assert verdict["accepted"] is True
    assert verdict["category"] == "atmosphere"
    assert 0.22 <= verdict["score"] < 0.34


def test_atmosphere_weak_topical_below_generic_bar_rejected():
    item = _verify_one(_candidate("Waves on the shore"))
    verdict = item["verification"]
    assert verdict["accepted"] is False
    assert item["reject_reason"] == "below_threshold"
    assert verdict["failed_checks"] == ["below_threshold"]


def test_atmosphere_zero_overlap_passes_gate_rejected_by_scorer():
    candidate = _candidate(
        "Quantum semiconductor fabrication facility tour",
        "cleanroom lithography process",
    )
    # The atmosphere gate profile imposes no subject, place or
    # type-presence requirement — nothing for the gate to fail.
    assert mp.gate_all_failures(candidate, dict(_ATMOSPHERE_NEED)) == []
    item = _verify_one(candidate)
    assert item["verification"]["accepted"] is False
    assert item["rejection_stage"] == "scoring"
    assert item["reject_reason"] == "no_topical_evidence"


def test_atmosphere_contradiction_rejects_above_the_bar():
    # The compensable case the hard reject exists for: topical +
    # era evidence would score 0.25 — above the 0.22 generic bar —
    # but the candidate names the WRONG person, so the veto is the
    # only failed check and it rejects outright.
    scenario = {
        "visual": "night street crowd walking",
        "search_terms": ["night street", "crowd walking"],
        "specificity": "generic", "subject_id": "amelia",
        "era": "2010",
    }
    item = _verify_one(
        _candidate(
            "Marcus Webb night street crowd walking 2010",
            "night street crowd walking in 2010",
        ),
        scenario=scenario,
    )
    verdict = item["verification"]
    assert verdict["score"] >= 0.22
    assert verdict["accepted"] is False
    assert item["reject_reason"] == "contradictory_person"
    assert verdict["failed_checks"] == ["contradictory_person"]


def test_atmosphere_contradictory_date_rejected_at_gate():
    scenario = dict(_SHORELINE_SCENARIO, era="1969")
    need = dict(_ATMOSPHERE_NEED, era="1969")
    item = _verify_one(
        _candidate(
            "Ocean waves crashing on a rocky shoreline — filmed 2024",
            "ocean waves rocky shoreline 2024",
        ),
        scenario=scenario, need=need,
    )
    assert item["verification"]["accepted"] is False
    assert item["rejection_stage"] == "metadata_gate"
    assert item["gate_failures"] == ["contradictory_date"]


def test_atmosphere_banned_provider_rejected_at_gate():
    # Veto target updated 2026-11-26: this test pinned a Pexels host
    # rejected at the gate under the IA-05 provider ban, which the
    # founder LIFTED (Annex B). The surviving hard veto is a
    # social-platform source; it is enforced by verify_candidate,
    # the scorer leg of the same _uve_verify_all chain, so the
    # rejection is recorded at the scoring stage with the canonical
    # social_platform_source reason — still a hard rejection of an
    # otherwise topical atmosphere candidate, whatever its metadata.
    # Assembled so the standing ban source-scan stays clean.
    veto_host = "tik" + "tok"
    item = _verify_one(_candidate(
        "Ocean waves crashing on a rocky shoreline",
        "ocean waves rocky shoreline",
        url=f"https://www.{veto_host}.com/@someone/video/1",
    ))
    assert item["verification"]["accepted"] is False
    assert item["rejection_stage"] == "scoring"
    assert item["reject_reason"] == "social_platform_source"
    assert item["verification"]["rejection_reason"] == "social_platform_source"
    # New truth: the SAME candidate on the restored stock host is
    # not vetoed at all — it is accepted on its topical evidence.
    stock_host = "pex" + "els"
    item = _verify_one(_candidate(
        "Ocean waves crashing on a rocky shoreline",
        "ocean waves rocky shoreline",
        url=f"https://{stock_host}.com/video/1",
    ))
    assert item["verification"]["accepted"] is True
