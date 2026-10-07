from services.media_pipeline import (
    _uve_scenario_search,
    _uve_verify_all,
    _uve_pick,
)
from services.visual_registry import get_registry

scenarios = [
    {
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
    },
    {
        "subject": "Apollo 11",
        "event": "Apollo 11 entering lunar orbit",
        "place": "lunar orbit",
        "era": "July 1969",
        "specificity": "exact",
        "kind": "archival",
        "visual": "Apollo 11 entering lunar orbit in July 1969",
        "visual_requirement": "Apollo 11 entering lunar orbit in July 1969",
        "search_terms": [
            "Apollo 11",
            "lunar orbit",
            "July 1969",
        ],
        "queries": [
            "Apollo 11 lunar orbit footage",
            "Apollo 11 lunar orbit NASA 1969",
        ],
    },
    {
        "subject": "Armstrong and Aldrin",
        "event": "Eagle lunar descent",
        "place": "Moon",
        "era": "July 1969",
        "specificity": "representative",
        "kind": "archival",
        "visual": "Eagle lunar module descending toward the Moon with Armstrong and Aldrin aboard",
        "visual_requirement": "Eagle lunar module descending toward the Moon with Armstrong and Aldrin aboard",
        "search_terms": [
            "Apollo 11",
            "Eagle",
            "Armstrong",
            "Aldrin",
            "Moon",
        ],
        "queries": [
            "Apollo 11 lunar module Eagle descent 1969",
            "Apollo 11 descent to the Moon NASA footage",
        ],
    },
]

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
        {
            "id": "eagle",
            "canonical": "Eagle",
            "type": "object",
        },
        {
            "id": "armstrong",
            "canonical": "Armstrong",
            "type": "person",
        },
        {
            "id": "aldrin",
            "canonical": "Aldrin",
            "type": "person",
        },
    ]
}

registry = get_registry("selection_test7")
used_keys = set()
cache = {}

print()
print("=== SEARCH + VERIFY + PICK ===")
print()

for index, scenario in enumerate(scenarios, 1):

    shot_need = {
        "shot_index": index,
        "required_subject": scenario["subject"],
        "required_type": "event",
        "event": scenario["event"],
        "place": scenario["place"],
        "era": scenario["era"],
        "specificity": scenario["specificity"],
        "visual_requirement": scenario["visual_requirement"],
        "queries": scenario["queries"],
        "search_terms": scenario["search_terms"],
    }

    print("=" * 80)
    print("SHOT", index)
    print("SUBJECT:", scenario["subject"])
    print("EVENT:", scenario["event"])
    print("PLACE:", scenario["place"])
    print()

    scored = _uve_scenario_search(
        scenario,
        cache,
        "selection_test7",
        shot_need=shot_need,
    )

    print("SEARCHED:", len(scored))

    verified = _uve_verify_all(
        scored,
        scenario,
        bible,
    )

    accepted = [
        candidate
        for candidate in verified
        if (candidate.get("verification") or {}).get("accepted")
    ]

    print("VERIFIED:", len(accepted))

    picked = _uve_pick(
        verified,
        scenario,
        registry,
        used_keys,
        limit=3,
        shot_need=shot_need,
    )

    print("PICKED:", len(picked))
    print()

    for candidate in picked:
        print("TITLE:", candidate.get("title"))
        print("SOURCE:", candidate.get("source"))
        print("URL:", candidate.get("url") or candidate.get("link"))
        print("STATUS:", candidate.get("verification_status"))
        print("SCORE:", candidate.get("universal_score"))
        print("REASONS:", (candidate.get("verification") or {}).get("reasons"))
        print("-" * 60)

print()
print("=== TEST COMPLETE ===")
