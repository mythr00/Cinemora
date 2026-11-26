"""Gate 4 regression tests (Production Recovery, 2026-11-26).

Shapes taken from the Hyatt Regency walkway collapse run
cdfcc130 (engineering disaster, 1981 event, still-standing
structure), whose asset stage verified only 6 assets for 22
sentences. Four misfire classes, each fixed without weakening the
genuine protection:

1. Co-located bible places (hotel / its atrium / its city) treated
   as mutually contradictory — at the scorer
   (story_understanding.detect_contradiction) and at the metadata
   gate (pipeline_assignment.gate_all_failures).
2. The unrelated-named-person extractor reading any title-case
   bigram as a person ("One Rod", "The Story", "Beneath Two").
   A genuine different person (Michael Bloomberg at a 9/11
   memorial) must still hard-reject.
3. B2: place/date tokens masquerading as topical evidence — a
   trains photograph "...at Kansas City in 1980" scored 1.0 for a
   hotel-opening sentence. Place/date-only topical matches now
   fall to no_topical_evidence; genuine content matches are
   scored exactly as before.
4. B5: a wholly frozen video segment (sampled frames identical)
   is rejected at frame verification as frozen_segment; moving
   segments and still images are untouched.

Plus the per-run verdict cache: an identical (candidate, need,
scenario) evaluation inside one activated run executes the gate
and scorer once; under the default context nothing is cached.
"""

import pytest
from PIL import Image

from services import frame_verification
from services import media_pipeline as mp
from services import pipeline_assignment as pa
from services import run_context
from services import story_understanding as story


def _hyatt_bible():
    return story.hydrate_bible({
        "entities": [
            {"id": "collapse", "canonical": "Hyatt Regency walkway collapse",
             "aliases": ["Kansas City collapse", "collapse", "walkway collapse"],
             "type": "event"},
            {"id": "rods", "canonical": "steel hanger rods",
             "aliases": ["steel rods", "hanger rods", "rods", "single rod",
                         "two shorter rod sets"], "type": "object"},
            {"id": "memorial", "canonical": "memorial to the 114 victims",
             "aliases": ["memorial"], "type": "object"},
            {"id": "hotel", "canonical": "Hyatt Regency Kansas City",
             "aliases": ["Hyatt Regency hotel", "the hotel"], "type": "place"},
            {"id": "atrium", "canonical": "Hyatt Regency atrium",
             "aliases": ["atrium"], "type": "place"},
            {"id": "kc", "canonical": "Kansas City, Missouri",
             "aliases": ["Kansas City"], "type": "place"},
            {"id": "union_station", "canonical": "Union Station",
             "aliases": [], "type": "place"},
            {"id": "gillum", "canonical": "Jack Gillum",
             "aliases": ["Gillum"], "type": "person"},
            {"id": "duncan", "canonical": "Daniel Duncan",
             "aliases": ["Duncan"], "type": "person"},
        ]
    })


def _scenario(**over):
    sc = {
        "subject_id": "rods",
        "specificity": "representative",
        "search_terms": ["steel hanger rods walkway collapse"],
        "visual": "Steel hanger rods supporting the suspended walkways",
        "era": "1981",
        "place": "Hyatt Regency atrium",
    }
    sc.update(over)
    return sc


# ------------------------------------------------------- 1. co-located places

def test_scorer_city_mention_is_not_contradictory_for_atrium_beat():
    # The KCUR memorial image shape: score 0.831 in the run, killed
    # by "contradictory_place: Kansas City, Missouri".
    verdict = story.verify_candidate(
        {
            "title": "Three Decades On, A Memorial For The Victims Of "
                     "The Hyatt Disaster | KCUR - Kansas City news",
            "snippet": "The memorial to the 114 victims of the Hyatt "
                       "skywalk collapse in Kansas City, Missouri",
        },
        _scenario(
            subject_id="memorial", place="Hyatt Regency atrium", era="2015",
            search_terms=["memorial to the 114 victims dedication 2015"],
            visual="The memorial to the 114 victims of the Hyatt collapse",
        ),
        _hyatt_bible(),
    )
    assert verdict["rejection_reason"] != "contradictory_place"
    assert verdict["accepted"] is True


def test_scorer_genuinely_different_bible_place_still_contradicts():
    verdict = story.verify_candidate(
        {
            "title": "Union Station hosts gala dinner",
            "snippet": "Crowds gathered inside Union Station for the event",
        },
        _scenario(
            subject_id="memorial", era="2015",
            search_terms=["memorial to the 114 victims dedication 2015"],
            visual="The memorial to the 114 victims of the Hyatt collapse",
        ),
        _hyatt_bible(),
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "contradictory_place"


def test_gate_city_mention_is_not_contradictory_for_atrium_need():
    need = {
        "required_subject": "Friday tea dance",
        "required_type": "event",
        "place": "Hyatt Regency atrium",
        "era": "1981",
        "specificity": "exact",
        "visual_category": "evidence",
        "other_places": [
            "Hyatt Regency Kansas City", "Kansas City, Missouri",
            "Union Station",
        ],
        "other_people": ["Jack Gillum", "Daniel Duncan"],
    }
    failures = pa.gate_all_failures(
        {
            "title": "1981 Hyatt Regency collapse: What happened",
            "snippet": "Kansas City's Hyatt Regency skywalk collapse explained",
        },
        need,
    )
    assert "contradictory_place" not in failures


def test_gate_genuinely_different_place_still_contradicts():
    need = {
        "required_subject": "Friday tea dance",
        "required_type": "event",
        "place": "Hyatt Regency atrium",
        "era": "1981",
        "specificity": "exact",
        "visual_category": "evidence",
        "other_places": [
            "Hyatt Regency Kansas City", "Kansas City, Missouri",
            "Union Station",
        ],
        "other_people": ["Jack Gillum", "Daniel Duncan"],
    }
    failures = pa.gate_all_failures(
        {
            "title": "Union Station gala draws crowds",
            "snippet": "Union Station hosted the annual gala dinner",
        },
        need,
    )
    assert "contradictory_place" in failures


# ------------------------------------------------------- 2. person extraction

@pytest.mark.parametrize("title", [
    "Hyatt Regency Walkway Collapse: One Rod Became Two",
    "The Story of The Hyatt Regency Walkway Collapse",
    "Hyatt Regency Collapse: The 4-Inch Offset Behind a $140M Payout",
    "They Danced Beneath Two Hanging Walkways. 114 Died.",
])
def test_title_case_phrases_are_not_persons(title):
    verdict = story.verify_candidate(
        {
            "title": title,
            "snippet": "How the Hyatt Regency walkway collapse happened: "
                       "steel hanger rods, the walkways and the rods",
        },
        _scenario(),
        _hyatt_bible(),
    )
    assert verdict["rejection_reason"] != "unrelated_named_person"
    assert verdict["accepted"] is True


def test_topical_phrase_matching_scenario_vocabulary_is_not_a_person():
    # "Structural Engineering" fired 24x in the run, under
    # engineering beats whose own scenario vocabulary is structural
    # engineering — topical language, not a person.
    verdict = story.verify_candidate(
        {
            "title": "Suspended Walkways and Structural Engineering Failures",
            "snippet": "Structural engineering analysis of suspended "
                       "walkways and the hanger rod failure",
        },
        _scenario(
            search_terms=["structural engineering failure suspended walkways"],
            visual="Structural engineering diagram of suspended walkways",
        ),
        _hyatt_bible(),
    )
    assert verdict["rejection_reason"] != "unrelated_named_person"
    assert verdict["accepted"] is True


@pytest.mark.parametrize("title", [
    # Second-bigram fallthrough shapes from the run: once the first
    # artefact pair is skipped, the extractor used to land on a
    # verb phrase or adverb pair in the same title.
    "The Hyatt Regency Disaster 114 People Died That Day",
    "How One Nut Brought Down Two Walkways (Hyatt Regency)",
    "City Hotel That Was Never Really Safe",
    "Hyatt Regency walkway collapse (1981) | Kansas City, Cause, & Death Toll",
])
def test_verb_and_adverb_pairs_are_not_persons(title):
    verdict = story.verify_candidate(
        {
            "title": title,
            "snippet": "How the Hyatt Regency walkway collapse happened: "
                       "steel hanger rods, the walkways and the rods",
        },
        _scenario(),
        _hyatt_bible(),
    )
    assert verdict["rejection_reason"] != "unrelated_named_person"
    assert verdict["accepted"] is True


def test_genuine_different_person_still_hard_rejected():
    # The run's own correct catch: a named person from a different
    # event (9/11 memorial) offered for the Hyatt memorial beat.
    verdict = story.verify_candidate(
        {
            "title": "Michael Bloomberg at 9/11 memorial dedication in New York",
            "snippet": "Michael Bloomberg speaks at the 9/11 memorial "
                       "dedication ceremony",
        },
        _scenario(
            subject_id="memorial", era="2015",
            search_terms=["memorial to the 114 victims dedication 2015"],
            visual="The memorial to the 114 victims of the Hyatt collapse",
        ),
        _hyatt_bible(),
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "unrelated_named_person"


def test_other_case_victim_name_still_hard_rejected():
    # The original run-9 shape (Tristyn Bailey): a different case's
    # victim named in the title, on a person-anchored production.
    bible = story.hydrate_bible({
        "entities": [
            {"id": "gabby", "canonical": "Gabby Petito",
             "aliases": ["Petito"], "type": "person"},
            {"id": "moab", "canonical": "Moab, Utah",
             "aliases": ["Moab"], "type": "place"},
        ]
    })
    verdict = story.verify_candidate(
        {
            "title": "Tristyn Bailey case explained",
            "snippet": "The Tristyn Bailey investigation, what we know",
        },
        {
            "subject_id": "gabby", "specificity": "representative",
            "search_terms": ["search for Gabby Petito Moab Utah"],
            "visual": "Search teams looking for Gabby Petito near Moab",
            "era": "2021", "place": "Moab, Utah",
        },
        bible,
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "unrelated_named_person"


# ------------------------------------------------------- 3. B2 place/date-only

def test_trains_photo_place_date_only_is_not_topical_evidence():
    # The exact B2 shape: tier-escalation scenario anchored on the
    # city, topical terms = city + year; the trains photo matched
    # both and scored 1.0 in the run.
    verdict = story.verify_candidate(
        {
            "title": "File:Conrail 6503 (SD40-2) and Missouri-Kansas-Texas "
                     "351 (U23B) at Kansas City in 1980.png",
            "snippet": "Conrail locomotives at Kansas City in 1980",
        },
        _scenario(
            subject_id="kc", place="Kansas City, Missouri", era="1980",
            search_terms=["Kansas City, Missouri 1980"],
            visual="Kansas City, Missouri in 1980",
        ),
        _hyatt_bible(),
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "no_topical_evidence"


def test_genuine_hotel_photo_still_passes_for_opening_sentence():
    verdict = story.verify_candidate(
        {
            "title": "Hyatt Regency Kansas City hotel, 1980",
            "snippet": "The Hyatt Regency hotel in Kansas City shortly "
                       "after it opened",
        },
        _scenario(
            subject_id="hotel", place="Hyatt Regency Kansas City",
            era="1980",
            search_terms=["Hyatt Regency Kansas City hotel opened 1980"],
            visual="The Hyatt Regency hotel in Kansas City shortly "
                   "after opening in 1980",
        ),
        _hyatt_bible(),
    )
    assert verdict["accepted"] is True
    assert verdict["contributions"]["topical"] > 0


# ------------------------------------------------------- 4. B5 frozen segment

def _frame(colour):
    return Image.new("RGB", (320, 180), colour)


def test_frozen_video_segment_rejected():
    record = frame_verification.verify_frames(
        [_frame((120, 30, 30))] * 3, kind="video",
    )
    assert record["outcome"] == "reject"
    assert record["reason"] == frame_verification.FROZEN_SEGMENT_REASON
    assert record["detectors"]["motion"]["fired"] is True


def test_moving_video_segment_not_frozen():
    record = frame_verification.verify_frames(
        [_frame((120, 30, 30)), _frame((30, 120, 30)), _frame((30, 30, 120))],
        kind="video",
    )
    assert record["detectors"]["motion"]["fired"] is False
    assert record["reason"] != frame_verification.FROZEN_SEGMENT_REASON


def test_still_image_is_never_frozen():
    record = frame_verification.verify_frames(
        [_frame((120, 30, 30))], kind="image",
    )
    assert record["detectors"]["motion"]["fired"] is False
    assert record["reason"] != frame_verification.FROZEN_SEGMENT_REASON


# ------------------------------------------------------- 5. verdict cache

def _cache_fixture():
    bible = story.hydrate_bible({
        "entities": [
            {"id": "berlin_wall", "canonical": "Berlin Wall",
             "aliases": ["the Wall"], "type": "object"},
            {"id": "east_berlin", "canonical": "East Berlin",
             "aliases": [], "type": "location"},
        ]
    })
    scenario = {
        "subject_id": "berlin_wall", "specificity": "representative",
        "search_terms": ["crowds crossing checkpoint"],
        "visual": "crowds of people crossing through an opened checkpoint",
        "era": "1989",
    }
    need = {
        "required_type": "event", "specificity": "representative",
        "visual_category": "evidence",
        "required_subject": "East Berlin", "place": "berlin",
    }
    candidates = [
        {"title": "Champagne wall rental", "snippet": "weddings",
         "url": "https://x.example/1"},
        {"title": "Berlin Wall crowds 1989", "snippet": "crowds crossing",
         "url": "https://x.example/2"},
    ]
    return candidates, scenario, bible, need


def test_verdict_cache_replays_within_activated_run(monkeypatch):
    calls = []
    real_gate = mp.gate_shot_need

    def counting_gate(candidate, shot_need):
        calls.append(1)
        return real_gate(candidate, shot_need)

    monkeypatch.setattr(mp, "gate_shot_need", counting_gate)
    candidates, scenario, bible, need = _cache_fixture()
    ctx = run_context.RunContext(run_id="gate4-test", project_id="gate4")
    previous = run_context.activate(ctx)
    try:
        first = mp._uve_verify_all(candidates, scenario, bible, shot_need=need)
        assert len(calls) == 2
        second = mp._uve_verify_all(candidates, scenario, bible, shot_need=need)
        assert len(calls) == 2  # replayed, gate not re-executed
        assert [c.get("reject_reason") for c in second] == [
            c.get("reject_reason") for c in first
        ]
    finally:
        run_context.deactivate(previous)


def test_no_verdict_cache_under_default_context(monkeypatch):
    calls = []
    real_gate = mp.gate_shot_need

    def counting_gate(candidate, shot_need):
        calls.append(1)
        return real_gate(candidate, shot_need)

    monkeypatch.setattr(mp, "gate_shot_need", counting_gate)
    candidates, scenario, bible, need = _cache_fixture()
    mp._uve_verify_all(candidates, scenario, bible, shot_need=need)
    mp._uve_verify_all(candidates, scenario, bible, shot_need=need)
    assert len(calls) == 4  # default context: every call evaluates
