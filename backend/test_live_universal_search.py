from services import media_pipeline as m


TESTS = [
    (
        "Kodak",
        "In 1975, Kodak engineer Steven Sasson invented the world's first digital camera in Rochester, New York.",
    ),
    (
        "Tsoede / Oyo",
        "In the fifteenth century, Tsoede rose to power and conquered Oyo.",
    ),
    (
        "Lagos crime",
        "Police searched the house in Lagos as investigators tried to reconstruct what happened.",
    ),
    (
        "D-Day / Normandy",
        "On June 6, 1944, Allied forces landed on the beaches of Normandy during the D-Day invasion.",
    ),
    (
        "Apollo",
        "NASA prepared the Apollo program for the historic mission to land humans on the Moon.",
    ),
    (
        "Titanic",
        "In 1912, the Titanic struck an iceberg in the North Atlantic and began to sink.",
    ),
    (
        "Tibet dam",
        "Engineers began construction of a massive dam on the Tibetan plateau across a major river.",
    ),
    (
        "Business bankruptcy",
        "The company eventually filed for bankruptcy after years of financial losses.",
    ),
]


def main():
    total = len(TESTS)
    passed = 0

    print("=" * 110)
    print("LIVE UNIVERSAL SEARCH TEST")
    print("=" * 110)

    for index, (name, sentence) in enumerate(TESTS, 1):
        print()
        print("-" * 110)
        print(f"TEST {index}/{total}: {name}")
        print(f"SENTENCE: {sentence}")
        print("-" * 110)

        try:
            # ------------------------------------------------------------
            # 1. Universal sentence analysis
            # ------------------------------------------------------------
            analysis = m._uve_analyze_sentence(sentence)

            print("\nANALYSIS:")
            print("  entities      :", analysis.get("entities"))
            print("  locations     :", analysis.get("locations"))
            print("  objects       :", analysis.get("objects"))
            print("  dates         :", analysis.get("dates"))
            print("  historical    :", analysis.get("historical_period"))
            print("  actions       :", analysis.get("actions"))
            print("  event         :", analysis.get("event"))
            print("  visual_types  :", analysis.get("visual_types"))
            print("  keywords      :", analysis.get("keywords"))

            # ------------------------------------------------------------
            # 2. Universal query generation
            # ------------------------------------------------------------
            queries = m._uve_make_queries_for_sentence(analysis)

            print("\nQUERIES:")
            for q in queries:
                print(f"  - {q}")

            if not queries:
                raise RuntimeError("No search queries were generated.")

            # ------------------------------------------------------------
            # 3. LIVE SEARCH
            #
            # IMPORTANT:
            # _uve_search_candidates requires:
            #     (analysis, search_cache)
            # ------------------------------------------------------------
            search_cache = {}

            candidates = m._uve_search_candidates(
                analysis,
                search_cache,
            )

            print(f"\nRAW CANDIDATES: {len(candidates)}")

            # ------------------------------------------------------------
            # 4. Rank candidates for every generated query
            # ------------------------------------------------------------
            ranked = []

            for query in queries:
                query_candidates = m._uve_rank_candidates(
                    candidates,
                    analysis,
                    query,
                )

                if query_candidates:
                    ranked.extend(query_candidates)

            # Remove duplicate candidate objects/assets where possible.
            deduped = []
            seen = set()

            for candidate in ranked:
                key = (
                    candidate.get("url")
                    or candidate.get("download_url")
                    or candidate.get("video_url")
                    or candidate.get("image_url")
                    or candidate.get("thumbnail")
                    or candidate.get("id")
                    or candidate.get("title")
                )

                if key in seen:
                    continue

                seen.add(key)
                deduped.append(candidate)

            ranked = deduped

            print(f"RANKED CANDIDATES: {len(ranked)}")

            # ------------------------------------------------------------
            # 5. Show strongest candidates BEFORE selection
            # ------------------------------------------------------------
            print("\nTOP CANDIDATES:")

            for rank, candidate in enumerate(ranked[:10], 1):
                score = candidate.get("_uve_score")
                if score is None:
                    score = candidate.get("score")

                title = (
                    candidate.get("title")
                    or candidate.get("name")
                    or candidate.get("description")
                    or ""
                )

                provider = (
                    candidate.get("source")
                    or candidate.get("provider")
                    or candidate.get("site")
                    or ""
                )

                query = (
                    candidate.get("_uve_query")
                    or candidate.get("matched_query")
                    or candidate.get("query")
                    or ""
                )

                verification = candidate.get("verification_status")

                print(f"\n  #{rank}")
                print(f"    score       : {score}")
                print(f"    provider    : {provider}")
                print(f"    query       : {query}")
                print(f"    verification: {verification}")
                print(f"    title       : {title[:220]}")

            # ------------------------------------------------------------
            # 6. Universal selection
            # ------------------------------------------------------------
            registry = {}
            used_asset_keys = set()

            video_candidates = [
                c
                for c in ranked
                if str(
                    c.get("media_type")
                    or c.get("type")
                    or c.get("kind")
                    or ""
                ).lower()
                in {
                    "video",
                    "videos",
                }
            ]

            image_candidates = [
                c
                for c in ranked
                if str(
                    c.get("media_type")
                    or c.get("type")
                    or c.get("kind")
                    or ""
                ).lower()
                in {
                    "image",
                    "images",
                    "photo",
                    "photos",
                }
            ]

            # If providers don't expose a media type, let the selector
            # receive the complete ranked pool as a fallback.
            if not video_candidates and not image_candidates:
                video_candidates = ranked

            selected_videos = m._uve_select_candidates(
                video_candidates,
                analysis,
                "video",
                registry,
                used_asset_keys,
                3,
            )

            selected_images = m._uve_select_candidates(
                image_candidates,
                analysis,
                "image",
                registry,
                used_asset_keys,
                3,
            )

            print("\nSELECTED VIDEOS:")
            print(f"  count: {len(selected_videos)}")

            for item in selected_videos:
                print(
                    "  -",
                    item.get("title")
                    or item.get("name")
                    or item.get("description")
                    or "",
                    "| score=",
                    item.get("_uve_score"),
                    "| provider=",
                    item.get("source") or item.get("provider"),
                    "| query=",
                    item.get("_uve_query")
                    or item.get("matched_query")
                    or item.get("query"),
                )

            print("\nSELECTED IMAGES:")
            print(f"  count: {len(selected_images)}")

            for item in selected_images:
                print(
                    "  -",
                    item.get("title")
                    or item.get("name")
                    or item.get("description")
                    or "",
                    "| score=",
                    item.get("_uve_score"),
                    "| provider=",
                    item.get("source") or item.get("provider"),
                    "| query=",
                    item.get("_uve_query")
                    or item.get("matched_query")
                    or item.get("query"),
                )

            # ------------------------------------------------------------
            # 7. Basic live-test success condition
            # ------------------------------------------------------------
            if not candidates:
                print("\nRESULT: FAIL — no live search candidates returned.")
                continue

            if not ranked:
                print("\nRESULT: FAIL — candidates returned but none ranked.")
                continue

            if not selected_videos and not selected_images:
                print("\nRESULT: FAIL — candidates ranked but none selected.")
                continue

            print("\nRESULT: PASS")
            passed += 1

        except Exception as exc:
            print(f"\nRESULT: ERROR — {type(exc).__name__}: {exc}")

    print()
    print("=" * 110)
    print(f"LIVE SEARCH RESULT: {passed}/{total} TESTS PASSED")
    print("=" * 110)


if __name__ == "__main__":
    main()