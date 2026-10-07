from services.script_analyzer import analyze_sentence
from services.media_pipeline import _uve_candidate_score

TESTS = [
    {
        "name": "KODAK",
        "sentence": "In 1975, Steven Sasson invented the first digital camera at Kodak in Rochester.",
        "correct": {
            "title": "Steven Sasson Kodak digital camera 1975",
            "description": "Steven Sasson with Kodak's early digital camera in Rochester in 1975",
            "query": "Steven Sasson Kodak 1975",
            "text": "Steven Sasson Kodak digital camera Rochester 1975 invention",
        },
        "wrong": {
            "title": "Florida police investigation",
            "description": "Police investigating a crime scene in Florida",
            "query": "Florida police investigation",
            "text": "Florida police crime investigation murder",
        },
    },
    {
        "name": "TSOEDE",
        "sentence": "Tsoede rose to power and conquered Oyo in the fifteenth century.",
        "correct": {
            "title": "Tsoede Oyo fifteenth century",
            "description": "Historical illustration of Tsoede and the Oyo conquest",
            "query": "Tsoede Oyo fifteenth century",
            "text": "Tsoede Oyo conquest fifteenth century history",
        },
        "wrong": {
            "title": "Steven Sasson Kodak digital camera",
            "description": "Steven Sasson demonstrating an early Kodak digital camera",
            "query": "Steven Sasson Kodak",
            "text": "Steven Sasson Kodak digital camera Rochester",
        },
    },
    {
        "name": "LAGOS CRIME",
        "sentence": "Investigators searched the victim's house in Lagos after the murder.",
        "correct": {
            "title": "Police search house Lagos",
            "description": "Investigators searching a house in Lagos after a murder",
            "query": "Lagos house investigation",
            "text": "Lagos investigators searching house crime scene murder",
        },
        "wrong": {
            "title": "Apollo spacecraft NASA",
            "description": "NASA engineers preparing the Apollo spacecraft",
            "query": "NASA Apollo",
            "text": "NASA Apollo spacecraft Moon preparation",
        },
    },
    {
        "name": "NORMANDY",
        "sentence": "American and British forces landed in Normandy on June 6, 1944.",
        "correct": {
            "title": "Normandy landing 1944",
            "description": "American and British forces landing in Normandy during World War II",
            "query": "Normandy landing 1944",
            "text": "Normandy D-Day landing June 6 1944 American British forces",
        },
        "wrong": {
            "title": "Titanic iceberg 1912",
            "description": "The Titanic striking an iceberg in the North Atlantic",
            "query": "Titanic iceberg 1912",
            "text": "Titanic iceberg North Atlantic 1912",
        },
    },
    {
        "name": "APOLLO",
        "sentence": "NASA engineers prepared the Apollo spacecraft for its journey to the Moon.",
        "correct": {
            "title": "NASA Apollo spacecraft",
            "description": "NASA engineers preparing the Apollo spacecraft for a Moon mission",
            "query": "NASA Apollo spacecraft",
            "text": "NASA Apollo spacecraft Moon mission preparation",
        },
        "wrong": {
            "title": "Tsoede Oyo conquest",
            "description": "Historical illustration of Tsoede conquering Oyo",
            "query": "Tsoede Oyo",
            "text": "Tsoede Oyo conquest fifteenth century",
        },
    },
    {
        "name": "TITANIC",
        "sentence": "The Titanic struck an iceberg in the North Atlantic in 1912.",
        "correct": {
            "title": "Titanic iceberg 1912",
            "description": "Titanic striking an iceberg in the North Atlantic in 1912",
            "query": "Titanic iceberg 1912",
            "text": "Titanic iceberg North Atlantic 1912 collision",
        },
        "wrong": {
            "title": "Kodak digital camera 1975",
            "description": "Steven Sasson with an early Kodak digital camera",
            "query": "Kodak digital camera",
            "text": "Steven Sasson Kodak digital camera Rochester 1975",
        },
    },
    {
        "name": "BANKRUPTCY",
        "sentence": "The company filed for bankruptcy after years of financial losses.",
        "correct": {
            "title": "Company bankruptcy financial losses",
            "description": "Corporate bankruptcy filing following years of financial losses",
            "query": "bankruptcy financial losses",
            "text": "company bankruptcy filing financial losses finance",
        },
        "wrong": {
            "title": "Normandy D-Day landing",
            "description": "Military forces landing in Normandy during World War II",
            "query": "Normandy landing",
            "text": "Normandy D-Day 1944 military landing",
        },
    },
    {
        "name": "TIBET DAM",
        "sentence": "The dam was constructed across the river on the Tibetan plateau.",
        "correct": {
            "title": "Tibetan plateau dam construction",
            "description": "Dam construction across a river on the Tibetan plateau",
            "query": "Tibet dam construction",
            "text": "Tibet Tibetan plateau dam river construction",
        },
        "wrong": {
            "title": "Apollo spacecraft NASA",
            "description": "NASA Apollo spacecraft preparing for a Moon mission",
            "query": "NASA Apollo",
            "text": "NASA Apollo spacecraft Moon preparation",
        },
    },
]


def candidate_score(candidate, analysis):
    try:
        return _uve_candidate_score(candidate, analysis)
    except TypeError:
        try:
            return _uve_candidate_score(candidate, analysis, analysis.get("search_queries", []))
        except TypeError:
            return _uve_candidate_score(
                candidate=candidate,
                analysis=analysis,
            )


print("=" * 90)
print("UNIVERSAL CANDIDATE VERIFIER TEST")
print("=" * 90)

passed = 0

for test in TESTS:
    analysis = analyze_sentence(test["sentence"])

    correct_score = candidate_score(test["correct"], analysis)
    wrong_score = candidate_score(test["wrong"], analysis)

    ok = correct_score > wrong_score

    if ok:
        passed += 1

    print("\n" + "-" * 90)
    print(test["name"])
    print("-" * 90)
    print("Correct score:", correct_score)
    print("Wrong score:  ", wrong_score)
    print("PASS:          ", ok)

print("\n" + "=" * 90)
print(f"RESULT: {passed}/{len(TESTS)} TESTS PASSED")
print("=" * 90)
