from services.script_analyzer import analyze_sentence
from services.media_pipeline import _uve_candidate_score


TESTS = [

    # ================================================================
    # KODAK — plausible but wrong
    # ================================================================
    {
        "name": "KODAK — wrong person",
        "sentence": "In 1975, Steven Sasson invented the first digital camera at Kodak in Rochester.",
        "correct": {
            "title": "Steven Sasson Kodak digital camera 1975",
            "description": "Steven Sasson with Kodak's first digital camera in Rochester in 1975",
            "query": "Steven Sasson Kodak 1975",
            "text": "Steven Sasson Kodak digital camera Rochester 1975 invention",
        },
        "wrong": {
            "title": "Steve Jobs digital photography",
            "description": "Steve Jobs discussing digital photography and Apple technology",
            "query": "Steve Jobs digital camera",
            "text": "Steve Jobs Apple digital photography technology",
        },
    },

    {
        "name": "KODAK — wrong era",
        "sentence": "In 1975, Steven Sasson invented the first digital camera at Kodak in Rochester.",
        "correct": {
            "title": "Steven Sasson Kodak digital camera 1975",
            "description": "Steven Sasson Kodak digital camera Rochester 1975",
            "query": "Steven Sasson 1975 Kodak",
            "text": "Steven Sasson Kodak 1975 digital camera invention",
        },
        "wrong": {
            "title": "Modern Kodak digital camera",
            "description": "A modern Kodak digital camera product photographed in a studio",
            "query": "Kodak digital camera",
            "text": "Kodak modern digital camera product photography",
        },
    },

    {
        "name": "KODAK — generic visual",
        "sentence": "In 1975, Steven Sasson invented the first digital camera at Kodak in Rochester.",
        "correct": {
            "title": "Steven Sasson 1975 invention",
            "description": "Steven Sasson and Kodak's early digital camera invention",
            "query": "Steven Sasson Kodak 1975",
            "text": "Steven Sasson Kodak Rochester 1975 invention digital camera",
        },
        "wrong": {
            "title": "Generic digital camera",
            "description": "Close-up footage of a generic digital camera",
            "query": "digital camera",
            "text": "digital camera photography electronics device",
        },
    },

    # ================================================================
    # TSOEDE — plausible historical material but wrong story
    # ================================================================
    {
        "name": "TSOEDE — wrong African history",
        "sentence": "Tsoede rose to power and conquered Oyo in the fifteenth century.",
        "correct": {
            "title": "Tsoede Oyo fifteenth century",
            "description": "Historical depiction of Tsoede and the conquest of Oyo",
            "query": "Tsoede Oyo fifteenth century",
            "text": "Tsoede Oyo conquest fifteenth century historical Yoruba",
        },
        "wrong": {
            "title": "Shaka Zulu historical battle",
            "description": "Historical illustration of Shaka Zulu and African warfare",
            "query": "Shaka Zulu African history",
            "text": "Shaka Zulu southern Africa kingdom warfare nineteenth century",
        },
    },

    {
        "name": "TSOEDE — wrong century",
        "sentence": "Tsoede rose to power and conquered Oyo in the fifteenth century.",
        "correct": {
            "title": "Tsoede Oyo conquest",
            "description": "Tsoede's fifteenth century conquest of Oyo",
            "query": "Tsoede Oyo conquest",
            "text": "Tsoede Oyo fifteenth century conquest",
        },
        "wrong": {
            "title": "Modern Oyo festival",
            "description": "Modern cultural festival in Oyo with traditional clothing",
            "query": "Oyo Nigeria",
            "text": "Oyo Nigeria modern festival Yoruba culture contemporary",
        },
    },

    # ================================================================
    # LAGOS CRIME — correct city but wrong visual
    # ================================================================
    {
        "name": "LAGOS — wrong event",
        "sentence": "Investigators searched the victim's house in Lagos after the murder.",
        "correct": {
            "title": "Lagos investigators search house",
            "description": "Investigators searching a victim's house in Lagos after a murder",
            "query": "Lagos house investigation",
            "text": "Lagos investigators police search house murder crime scene",
        },
        "wrong": {
            "title": "Lagos traffic",
            "description": "Traffic and pedestrians in central Lagos",
            "query": "Lagos Nigeria",
            "text": "Lagos Nigeria traffic road pedestrians city",
        },
    },

    {
        "name": "LAGOS — wrong location",
        "sentence": "Investigators searched the victim's house in Lagos after the murder.",
        "correct": {
            "title": "Lagos crime scene house",
            "description": "Police investigators searching a house in Lagos",
            "query": "Lagos crime scene house",
            "text": "Lagos police investigators house crime scene murder",
        },
        "wrong": {
            "title": "Abuja police investigation",
            "description": "Nigerian police investigating a crime in Abuja",
            "query": "Abuja police investigation",
            "text": "Abuja Nigeria police investigators crime investigation",
        },
    },

    # ================================================================
    # NORMANDY — right event, wrong place
    # ================================================================
    {
        "name": "NORMANDY — wrong landing",
        "sentence": "American and British forces landed in Normandy on June 6, 1944.",
        "correct": {
            "title": "Normandy D-Day landing 1944",
            "description": "American and British forces landing in Normandy on D-Day",
            "query": "Normandy landing 1944",
            "text": "Normandy D-Day June 6 1944 American British landing",
        },
        "wrong": {
            "title": "Pacific landing 1944",
            "description": "Allied troops landing on a Pacific island during World War II",
            "query": "WWII amphibious landing 1944",
            "text": "World War II Allied troops amphibious landing Pacific 1944",
        },
    },

    # ================================================================
    # APOLLO — right organization, wrong mission
    # ================================================================
    {
        "name": "APOLLO — NASA but wrong mission",
        "sentence": "NASA engineers prepared the Apollo spacecraft for its journey to the Moon.",
        "correct": {
            "title": "NASA Apollo Moon mission",
            "description": "NASA engineers preparing the Apollo spacecraft for a Moon mission",
            "query": "NASA Apollo spacecraft Moon",
            "text": "NASA Apollo spacecraft Moon mission preparation",
        },
        "wrong": {
            "title": "NASA Space Shuttle",
            "description": "NASA engineers preparing a Space Shuttle for launch",
            "query": "NASA Space Shuttle",
            "text": "NASA engineers Space Shuttle launch preparation spacecraft",
        },
    },

    # ================================================================
    # TITANIC — right ship category, wrong ship
    # ================================================================
    {
        "name": "TITANIC — generic ocean liner",
        "sentence": "The Titanic struck an iceberg in the North Atlantic in 1912.",
        "correct": {
            "title": "Titanic iceberg 1912",
            "description": "The Titanic striking an iceberg in the North Atlantic in 1912",
            "query": "Titanic iceberg 1912",
            "text": "Titanic iceberg North Atlantic 1912 collision",
        },
        "wrong": {
            "title": "Ocean liner at sea",
            "description": "A generic ocean liner sailing across the Atlantic",
            "query": "ocean liner Atlantic",
            "text": "ocean liner ship Atlantic sea voyage passenger ship",
        },
    },

    # ================================================================
    # DAM — right country, wrong infrastructure
    # ================================================================
    {
        "name": "TIBET — wrong Tibetan visual",
        "sentence": "The dam was constructed across the river on the Tibetan plateau.",
        "correct": {
            "title": "Tibet dam river construction",
            "description": "Dam construction across a river on the Tibetan plateau",
            "query": "Tibet dam construction",
            "text": "Tibet Tibetan plateau dam river construction infrastructure",
        },
        "wrong": {
            "title": "Tibetan monastery",
            "description": "A Buddhist monastery on the Tibetan plateau",
            "query": "Tibetan plateau",
            "text": "Tibetan plateau monastery Buddhism mountains Tibet culture",
        },
    },

    # ================================================================
    # BANKRUPTCY — generic but relevant vs unrelated finance
    # ================================================================
    {
        "name": "BANKRUPTCY — wrong financial event",
        "sentence": "The company filed for bankruptcy after years of financial losses.",
        "correct": {
            "title": "Corporate bankruptcy filing",
            "description": "A company filing for bankruptcy after years of financial losses",
            "query": "corporate bankruptcy financial losses",
            "text": "company bankruptcy filing financial losses corporate finance",
        },
        "wrong": {
            "title": "Stock market rally",
            "description": "Investors celebrating a strong stock market rally",
            "query": "stock market finance",
            "text": "stocks investors market rally finance Wall Street",
        },
    },
]


def score(candidate, analysis):
    return _uve_candidate_score(candidate, analysis, candidate.get("_uve_query") or candidate.get("matched_query") or candidate.get("query") or "")


print("=" * 100)
print("ADVERSARIAL UNIVERSAL CANDIDATE VERIFIER TEST")
print("=" * 100)

passed = 0
failed = 0

for test in TESTS:
    analysis = analyze_sentence(test["sentence"])

    correct_score = score(test["correct"], analysis)
    wrong_score = score(test["wrong"], analysis)

    ok = correct_score > wrong_score

    if ok:
        passed += 1
    else:
        failed += 1

    print("\n" + "-" * 100)
    print(test["name"])
    print("-" * 100)
    print(f"Correct: {correct_score:.1f}")
    print(f"Wrong:   {wrong_score:.1f}")
    print(f"Gap:     {correct_score - wrong_score:.1f}")
    print(f"PASS:    {ok}")

print("\n" + "=" * 100)
print(f"RESULT: {passed}/{len(TESTS)} PASSED")
print(f"FAILED: {failed}")
print("=" * 100)

