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
named builders in MISLEADING; Phase 4 materialised the remaining
M02/M04-M09/M12 factories.
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


def _m02_right_country_wrong_event():
    """M02: right place + era + broad topic, WRONG event — a campus
    fire/protest in the same city and year as the arrest beat. Event
    tokens for the required event are absent, so it must score below
    (and never be selected over) the correct-event candidate."""
    return candidate(
        title="Campus fire protest in Moscow, Idaho, 2022",
        snippet="Fire crews and a protest outside a house in Moscow in 2022",
        description="fire protest house Moscow 2022",
        url="https://www.youtube.com/watch?v=WRONGEVEN2",
        video_url="https://www.youtube.com/watch?v=WRONGEVEN2",
    )


def _m04_generic_stock():
    """M04: generic stock-style footage — a topical token or two
    (police) but NO subject, place or event tokens. Fails the gate
    for a named-person beat and can never be direct evidence."""
    return candidate(
        title="Police cars on a night street",
        snippet="Police cars patrol a city street at night",
        description="police cars street night",
        url="https://www.youtube.com/watch?v=GENERIC001",
        video_url="https://www.youtube.com/watch?v=GENERIC001",
    )


def _m05_wrong_era():
    """M05: right person, right place, right event — era decades
    off (1985 for a 2022 beat). A positive date contradiction."""
    return candidate(
        title="Bryan Kohberger arrest in Moscow, Idaho, 1985",
        snippet="Police arrest Bryan Kohberger outside the house in Moscow in 1985",
        description="Arrest police house Moscow 1985",
        url="https://www.youtube.com/watch?v=WRONGERA01",
        video_url="https://www.youtube.com/watch?v=WRONGERA01",
    )


def _m06_high_quality_unrelated():
    """M06: high-quality unrelated video — long duration, prestige
    source domain, zero topical/subject/place/event tokens. Source
    quality and production polish must not rescue irrelevance."""
    return candidate(
        title="Ocean giants: a prestige nature documentary",
        snippet="Award winning ocean documentary in high resolution",
        description="ocean whales documentary",
        url="https://www.bbc.co.uk/programmes/ocean1",
        video_url="https://www.bbc.co.uk/programmes/ocean1",
        source="bbc.co.uk",
        provider="bbc.co.uk",
        duration_seconds=3600,
        duration="60:00",
    )


def _m07_unknown_rights():
    """M07: fully relevant candidate whose licence is honestly
    "unknown" (the candidate() default). Selected under allow_all
    and recorded "unknown" — never upgraded, never blocked."""
    return candidate()


def _m08_failing_verification():
    """M08: metadata-relevant long video whose segment evidence is
    "none" (no timed evidence for the chosen window). This is the
    post-segment-evidence shape: it models the state the segment
    stage stamps, not a discovery record, and such an asset must
    never reach a timeline as verified segment evidence."""
    return candidate(
        segment_evidence="none",
        duration_seconds=3600,
        duration="60:00",
        url="https://www.youtube.com/watch?v=NOEVIDENC1",
        video_url="https://www.youtube.com/watch?v=NOEVIDENC1",
    )


def _m09_contextual_only():
    """M09: contextual-only asset — era/place-related to the arrest
    beat (Moscow, Idaho, 2022) and verifiable against a PLACE beat,
    but it names no required person/event, so for the person beat it
    is usable only as labelled contextual/rotation material."""
    return candidate(
        title="Moscow, Idaho in 2022",
        snippet="Views of Moscow, Idaho streets and police house in 2022",
        description="Moscow streets 2022",
        url="https://www.youtube.com/watch?v=CONTEXT001",
        video_url="https://www.youtube.com/watch?v=CONTEXT001",
    )


def _m12_duplicate_segment():
    """M12: duplicate-segment candidate — the SAME canonical video
    as candidate() (VALIDID001) with a clip_start overlapping the
    first segment's 0-8s range. The registry must block the overlap
    while allowing a non-overlapping second segment."""
    return candidate(clip_start=4, duration_seconds=8)


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
    """M11: plausible metadata, hard-vetoed provider.

    Originally a Pexels candidate (the IA-05 provider ban); the
    founder lifted that ban on 2026-11-26 (Annex B), so M11 now
    carries the surviving hard veto — a social-platform source
    (TikTok label AND host), rejected by the verification chain
    however plausible its metadata looks."""
    return candidate(
        provider="tik" + "tok",
        source="tik" + "tok",
        url="https://www." + "tik" + "tok" + ".com/@someone/video/1",
        video_url="https://www." + "tik" + "tok" + ".com/@someone/video/1",
    )


MISLEADING = {
    "M01": _m01_wrong_person,
    "M02": _m02_right_country_wrong_event,
    "M03": _m03_wrong_date,
    "M04": _m04_generic_stock,
    "M05": _m05_wrong_era,
    "M06": _m06_high_quality_unrelated,
    "M07": _m07_unknown_rights,
    "M08": _m08_failing_verification,
    "M09": _m09_contextual_only,
    "M10": _m10_same_video_other_spelling,
    "M11": _m11_banned_provider,
    "M12": _m12_duplicate_segment,
}

BEATS = {
    "B01": "named person, exact (scenario()/shot_need() defaults)",
    "B02": "historical event, exact (scenario with era)",
    "B03": "location, exact (scenario subject_id='moscow_idaho')",
    "B04": "date/timeline beat (targeted-graphics path)",
    "B08": "broad context / generic (scenario(specificity='generic'))",
    "B09": "scarce-footage beat (recovery tests in test_degraded_states)",
    "B10": "stills-appropriate beat (format_intent photograph)",
    "B11": "moving-footage-required beat (format_intent footage)",
    "B12": "no-acceptable-media beat (pool of misleading candidates only)",
}
