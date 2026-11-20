"""Shared synthetic fixtures for the Quality Recovery test programme.

Historical filename kept per the programme brief; the fixtures describe
VISUAL-QUALITY and FAILURE-OBSERVABILITY behaviour — no degraded mode
is restored or modelled here. Pure dicts, no I/O, no live providers.
Candidate dicts match the real discovery schema (see
DEGRADED_MODE_TEST_SPECIFICATION.md §1); pipeline-stamped fields
(verification, candidate_id, ...) are never pre-stamped on discovery
fixtures.

Beat registry B01-B13 and misleading-candidate registry M01-M12 from
the test specification are represented by the factories below plus the
named builders in MISLEADING; only the entries the Phase 1/2 tests use
are materialised.
"""

from services import story_understanding as story


def bible(entities=None):
    """A small case bible: one required person, one other person, two
    places, two events — enough for every contradiction class."""
    if entities is None:
        entities = [
            {"id": "bryan_kohberger", "canonical": "Bryan Kohberger",
             "type": "person", "aliases": ["Kohberger"], "role": "",
             "pronoun": None, "description": ""},
            {"id": "kaylee_goncalves", "canonical": "Kaylee Goncalves",
             "type": "person", "aliases": ["Goncalves"], "role": "",
             "pronoun": None, "description": ""},
            {"id": "moscow_idaho", "canonical": "Moscow, Idaho",
             "type": "place", "aliases": ["Moscow"], "role": "",
             "pronoun": None, "description": ""},
            {"id": "pullman_washington", "canonical": "Pullman, Washington",
             "type": "place", "aliases": ["Pullman"], "role": "",
             "pronoun": None, "description": ""},
            {"id": "kohberger_arrest", "canonical": "Kohberger arrest",
             "type": "event", "aliases": ["arrest"], "role": "",
             "pronoun": None, "description": ""},
            {"id": "campus_vigil", "canonical": "campus vigil",
             "type": "event", "aliases": ["vigil"], "role": "",
             "pronoun": None, "description": ""},
        ]
    return story.hydrate_bible({
        "entities": entities,
        "primary_subject_id": "bryan_kohberger",
        "title": "Idaho student murders",
        "story_type": "true_crime",
        "time_period": "2022",
    })


def scenario(**over):
    """B01: named person, exact, event + place + era set."""
    sc = {
        "subject_id": "bryan_kohberger",
        "subject": "Bryan Kohberger",
        "specificity": "exact",
        "kind": "event",
        "event": "arrest",
        "place": "Moscow, Idaho",
        "era": "2022",
        "visual": "police arrest outside the house in Moscow",
        "search_terms": ["arrest", "police", "house"],
        "queries": ["Bryan Kohberger arrest Moscow Idaho 2022"],
    }
    sc.update(over)
    return sc


def shot_need(**over):
    need = {
        "beat_id": "beat_01",
        "shot_index": 1,
        "shot_type": "event",
        "specificity": "exact",
        "description": "police arrest outside the house in Moscow",
        "required_subject_id": "bryan_kohberger",
        "required_subject": "Bryan Kohberger",
        "required_type": "person",
        "era": "2022",
        "place": "Moscow, Idaho",
        "event": "arrest",
        "visual_requirement": "police arrest outside the house in Moscow",
        "forbidden": [],
        "priority": 1,
        "queries": ["Bryan Kohberger arrest Moscow Idaho 2022"],
    }
    need.update(over)
    return need


def candidate(**over):
    """A fully relevant, verified-shape discovery candidate (M07
    rights shape: licence unknown, allowed under allow_all)."""
    cand = {
        "title": "Bryan Kohberger arrest in Moscow, Idaho, 2022",
        "snippet": "Police arrest Bryan Kohberger outside the house in Moscow in 2022",
        "description": "Arrest police house Moscow 2022",
        "url": "https://www.youtube.com/watch?v=VALIDID001",
        "video_url": "https://www.youtube.com/watch?v=VALIDID001",
        "source": "youtube",
        "provider": "youtube",
        "license": "unknown",
    }
    cand.update(over)
    return cand


def _m01_wrong_person():
    """M01: correct topic/era, WRONG bible person (the compensation
    hole: topical 0.45 + era 0.10 - 0.30 = 0.25 >= generic 0.22)."""
    return candidate(
        title="Kaylee Goncalves arrest police house 2022",
        snippet="Kaylee Goncalves arrest police house Moscow 2022",
        description="arrest police house 2022",
        url="https://www.youtube.com/watch?v=WRONGPERS1",
        video_url="https://www.youtube.com/watch?v=WRONGPERS1",
    )


def _m03_wrong_date():
    """M03: right event keywords, explicit contradictory year."""
    return candidate(
        title="Bryan Kohberger arrest in Moscow, Idaho, 2019",
        snippet="Police arrest Bryan Kohberger outside the house in Moscow in 2019",
        description="Arrest police house Moscow 2019",
        url="https://www.youtube.com/watch?v=WRONGDATE1",
        video_url="https://www.youtube.com/watch?v=WRONGDATE1",
    )


def _m10_same_video_other_spelling():
    """M10: the same video under a different URL spelling."""
    return candidate(
        url="https://youtu.be/VALIDID001?utm_source=share",
        video_url="https://youtu.be/VALIDID001?utm_source=share",
    )


def _m11_banned_provider():
    """M11: plausible metadata, banned provider."""
    return candidate(
        provider="pex" + "els",
        source="pex" + "els",
        url="https://videos.example.org/banned1",
        video_url="https://videos.example.org/banned1",
    )


MISLEADING = {
    "M01": _m01_wrong_person,
    "M03": _m03_wrong_date,
    "M10": _m10_same_video_other_spelling,
    "M11": _m11_banned_provider,
}

BEATS = {
    "B01": "named person, exact (scenario()/shot_need() defaults)",
    "B02": "historical event, exact (scenario with era)",
    "B08": "broad context / generic (scenario(specificity='generic'))",
    "B09": "scarce-footage beat (recovery tests in test_degraded_states)",
    "B12": "no-acceptable-media beat (pool of misleading candidates only)",
}
