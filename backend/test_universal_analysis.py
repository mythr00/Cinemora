from services.script_analyzer import analyze_sentence

tests = [
    "In 1975, Steven Sasson invented the first digital camera at Kodak in Rochester.",
    "Tsoede rose to power and conquered Oyo in the fifteenth century.",
    "Investigators searched the victim's house in Lagos after the murder.",
    "American and British forces landed in Normandy on June 6, 1944.",
    "NASA engineers prepared the Apollo spacecraft for its journey to the Moon.",
    "The Titanic struck an iceberg in the North Atlantic in 1912.",
    "The company filed for bankruptcy after years of financial losses.",
    "The dam was constructed across the river on the Tibetan plateau.",
]

for i, text in enumerate(tests, 1):
    result = analyze_sentence(text)

    print("\n" + "=" * 80)
    print(f"TEST {i}")
    print("=" * 80)
    print("TEXT:", text)
    print("ENTITIES:", result.get("entities"))
    print("LOCATIONS:", result.get("locations"))
    print("OBJECTS:", result.get("objects"))
    print("DATES:", result.get("dates"))
    print("PERIOD:", result.get("historical_period"))
    print("ACTIONS:", result.get("actions"))
    print("EVENT:", result.get("event"))
    print("VISUAL TYPES:", result.get("visual_types"))
    print("QUERIES:", result.get("search_queries"))
