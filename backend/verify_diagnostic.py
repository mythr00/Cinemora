from services.media_pipeline import _uve_scenario_search
from services import story_understanding as story

scenario = {
    "subject": "Apollo 11",
    "event": "Apollo 11 reaching the Moon",
    "place": "the Moon",
    "era": "July 19, 1969",
    "specificity": "exact",
    "kind": "archival",
    "visual": "Apollo 11 spacecraft approaching the Moon in July 1969",
    "visual_requirement": "Apollo 11 spacecraft approaching the Moon in July 1969",
    "search_terms": [
        "Apollo 11",
        "Moon",
        "July 1969",
    ],
    "queries": [
        "Apollo 11 Moon arrival 1969",
        "Apollo 11 July 19 1969 NASA footage",
    ],
}

bible = {
    "entities": [
        {
            "id": "apollo_11",
            "canonical": "Apollo 11",
            "type": "event",
        },
        {
            "id": "moon",
            "canonical": "Moon",
            "type": "place",
        },
    ]
}

cache = {}

print()
print("=== RAW SEARCH CANDIDATES ===")
print()

candidates = _uve_scenario_search(
    scenario,
    cache,
    "diagnostic_test",
    shot_need=scenario,
)

print("COUNT:", len(candidates))
print()

for i, candidate in enumerate(candidates, 1):

    print("=" * 80)
    print("CANDIDATE", i)

    for key in (
        "title",
        "source",
        "url",
        "snippet",
        "description",
        "matched_query",
    ):
        print(f"{key.upper()}:")
        print(candidate.get(key))

    print()
    print("=== VERIFICATION ===")

    verdict = story.verify_candidate(
        candidate,
        scenario,
        bible,
    )

    print("ACCEPTED:", verdict.get("accepted"))
    print("SCORE:", verdict.get("score"))

    for reason in verdict.get("reasons", []):
        print(" -", reason)

print()
print("=== END DIAGNOSTIC ===")
