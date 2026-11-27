"""Phase 9 creative test suite — chain-level items 16–18
(Motion Graphics Programme, 2026-11-27).

Item 16 (footage-dominant): a chain whose beats are covered by
verified footage produces NO full-frame graphics for those beats;
the single starved, data-backed beat is the only manifested
graphic, and the plan summary records that rarity (6 footage
treatments vs 1 manifested graphic). Items 17 (density budget)
and 18 (OFF gate) are pinned in test_phase6_7_selection.py and
test_p0_removal_pins.py / test_phase5_core_treatments.py; this
file adds only the footage-dominant evidence those files lack.
"""

from services import visual_director
from services.genre_packs import load_pack


def _plan_v1(rows):
    return {
        "version": 1, "project_id": "proj-suite-chain",
        "analysis_mode": "llm", "scene_governor": None,
        "sentences": rows,
    }


def _row(number, scene=1, fallback_kind="statistic"):
    return {
        "sentence_number": number,
        "scene_number": scene,
        "role": "statement",
        "primary_visual_intent": "exact",
        "secondary_visual_intents": [],
        "intent_queries": {},
        "graphic_fallback": {
            "allowed": True, "kind": fallback_kind,
        },
        "preferred_media_type": "video",
        "specificity_requirement": "exact",
        "visual_misrepresentation_risk": "low",
        "intents_source": "llm_plan",
        "fallback_concepts": None,
        "concepts_source": None,
        "entry_tier": "direct",
    }


def _outcome(number, scene=1, index=0, covered=False, starved=False,
             text="", scenario=None):
    return {
        "sentence_number": number,
        "scene_number": scene,
        "sentence_index": index,
        "text": text,
        "scenario": scenario or {},
        "covered": covered,
        "starved": starved,
        "tier": "direct" if covered else None,
        "asset_title": None,
        "asset_file": "/media/a.mp4" if covered else None,
        "scene_photo_count": 0,
        "secondary_video": None,
        "scene_has_identity_photo": False,
    }


def _decisions(plan_v2):
    return {
        row["sentence_number"]: row["decision"]
        for row in plan_v2["sentences"]
    }


def test_footage_dominant_chain_produces_no_full_frame_graphics():
    """Item 16: six covered beats + one starved chart-backed beat.

    Every covered beat resolves to a plain footage treatment —
    the only composition is the identification name super (a
    lower-third over footage, not a full-frame graphic). The one
    manifested full-frame graphic belongs to the starved beat,
    and the summary's counts record the rarity: 6 footage
    treatments, 1 manifested graphic.
    """
    pack = load_pack("general")
    bible = {
        "entities": [{
            "id": "p1", "type": "person",
            "canonical": "Ada Lovelace", "role": "Mathematician",
        }],
        "timeline": [],
    }
    rows = [_row(n) for n in range(1, 8)]
    rows[6] = _row(7, fallback_kind=None)
    plan = _plan_v1(rows)
    outcomes = {
        1: _outcome(1, covered=True, index=0, scenario={
            "subject": "p1", "subject_name": "Ada Lovelace",
        }),
    }
    for n in range(2, 7):
        outcomes[n] = _outcome(n, covered=True, index=n - 1,
                               text=f"Covered beat {n}.")
    outcomes[7] = _outcome(7, starved=True, index=6,
                           text="Output grew steadily.",
                           scenario={"chart": {
                               "series": [
                                   {"label": "1900", "value": 3},
                                   {"label": "1910", "value": 5},
                               ],
                               "units": "tonnes", "source": "Ledger",
                           }})
    plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=pack, bible=bible
    )
    decisions = _decisions(plan_v2)
    # Covered beats: footage treatment, no full-frame graphic.
    for n in range(1, 7):
        assert decisions[n]["treatment"] == "footage", n
        assert decisions[n]["reason"] == "verified_visual_assigned"
    # The only composition on a covered beat is the name-super
    # lower third — an over-footage card, not a full-frame entry.
    assert decisions[1]["composition"]["pattern"] == "lower_third_card"
    # Exactly one manifested graphic, for the starved beat only.
    assert len(manifest["entries"]) == 1
    assert manifest["entries"][0]["story_problem"] == "measurement"
    # The summary reflects the rarity of graphics in this chain.
    summary = plan_v2["summary"]
    assert summary["treatments"]["footage"] == 6
    assert summary["graphics_manifested"] == 1
    assert summary["compositions"] == 1
