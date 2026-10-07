from pathlib import Path

from services.script_analyzer import analyze_script
from services.media_pipeline import process_sentence_media


SCRIPT = """
FTX was once one of the biggest cryptocurrency exchanges in the world.
Customers rushed to withdraw their money.
The company eventually filed for bankruptcy.
"""


PROJECT_DIR = Path(
    "media/test-sentence-assets"
)


def main():
    print("=" * 80)
    print("SENTENCE-LEVEL ASSET MANAGER TEST")
    print("=" * 80)

    analysis = analyze_script(SCRIPT)

    sentences = analysis.get(
        "sentences",
        [],
    )

    print()
    print(
        f"Sentences found: {len(sentences)}"
    )

    if not sentences:
        print(
            "ERROR: Analyzer returned no sentences."
        )
        return

    for sentence in sentences:
        print()
        print("-" * 70)
        print(
            f"SENTENCE {sentence.get('sentence_id')}"
        )
        print(
            f"Text: {sentence.get('text')}"
        )
        print(
            f"Entities: {sentence.get('entities')}"
        )
        print(
            f"Actions: {sentence.get('actions')}"
        )
        print("Queries:")

        for query in sentence.get(
            "search_queries",
            [],
        ):
            print(
                f"  - {query}"
            )

    print()
    print("=" * 80)
    print("STARTING ASSET MANAGER")
    print("=" * 80)

    # Test every sentence.
    # Only 1 image + 1 video per sentence.
    for sentence in sentences:

        result = process_sentence_media(
            sentence=sentence,
            project_dir=str(PROJECT_DIR),
            scene_number=1,
            image_count=1,
            video_count=1,
        )

        print()
        print(
            f"SENTENCE "
            f"{sentence.get('sentence_id')} "
            f"RESULT"
        )

        print(
            f"Images: "
            f"{len(result.get('images', []))}"
        )

        print(
            f"Videos: "
            f"{len(result.get('videos', []))}"
        )

    print()
    print("=" * 80)
    print("ASSET MANAGER TEST COMPLETE")
    print("=" * 80)

    print()
    print(
        "Assets saved to:"
    )

    print(
        PROJECT_DIR.resolve()
    )


if __name__ == "__main__":
    main()