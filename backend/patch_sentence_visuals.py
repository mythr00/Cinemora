from pathlib import Path

path = Path(r".\services\media_pipeline.py")
text = path.read_text(encoding="utf-8")

old = """    sentence_results = []

    for sentence in sentences:

        sentence_number = get_sentence_number(
            sentence,
            1,
        )

        sentence_text = get_sentence_text(
            sentence
        )

        sentence_results.append(
            {
                "sentence_number": sentence_number,
                "sentence_text": sentence_text,
                "images": images.copy(),
                "videos": videos.copy(),
                "visuals": all_visuals.copy(),
                "sentence": sentence,
                "asset_scope": "scene_reuse",
            }
        )"""

new = """    sentence_results = []

    # --------------------------------------------------------
    # SENTENCE-AWARE VISUAL ASSIGNMENT
    # --------------------------------------------------------
    #
    # Do NOT give every sentence the entire scene image pool.
    # Each sentence gets its best matching downloaded image
    # when available, with the scene pool used as fallback.
    # --------------------------------------------------------

    scene_image_pool = list(images)
    scene_visual_pool = list(all_visuals)

    for sentence_index, sentence in enumerate(sentences):

        sentence_number = get_sentence_number(
            sentence,
            sentence_index + 1,
        )

        sentence_text = get_sentence_text(sentence)

        sentence_candidates = []

        sentence_queries = get_sentence_queries(sentence)

        if sentence_queries:
            try:
                sentence_candidates = collect_image_candidates(
                    sentence,
                    sentence_queries,
                )
            except Exception as error:
                print(
                    f"Sentence {sentence_number} "
                    f"image candidate search failed: {error}"
                )
                sentence_candidates = []

        try:
            sentence_candidates = apply_asset_quality_gate(
                sentence_candidates,
                sentence,
            )
        except Exception as error:
            print(
                f"Sentence {sentence_number} "
                f"quality gate failed: {error}"
            )
            sentence_candidates = []

        sentence_images = []

        for candidate in sentence_candidates[:3]:

            candidate_url = result_url(candidate)

            if not candidate_url:
                continue

            matched = None

            for image_item in scene_image_pool:

                if not isinstance(image_item, dict):
                    continue

                existing_url = (
                    image_item.get("image_url")
                    or image_item.get("source_url")
                    or ""
                )

                if existing_url == candidate_url:
                    matched = dict(image_item)
                    break

            if matched is None:
                continue

            matched["sentence_index"] = sentence_index
            matched["sentence_number"] = sentence_number
            matched["matched_sentence"] = sentence_text
            matched["sentence_score"] = candidate.get(
                "_asset_score",
                candidate.get("quality_score", 0),
            )
            matched["asset_scope"] = "sentence_match"

            sentence_images.append(matched)

        # Scene-level fallback.
        if not sentence_images and scene_image_pool:

            fallback_index = (
                sentence_index % len(scene_image_pool)
            )

            fallback = dict(
                scene_image_pool[fallback_index]
            )

            fallback["sentence_index"] = sentence_index
            fallback["sentence_number"] = sentence_number
            fallback["matched_sentence"] = sentence_text
            fallback["sentence_score"] = 0
            fallback["asset_scope"] = "scene_fallback"

            sentence_images.append(fallback)

        sentence_visuals = []

        for image_item in sentence_images:

            visual_item = dict(image_item)
            visual_item["visual_type"] = "image"

            sentence_visuals.append(
                visual_item
            )

        sentence_results.append(
            {
                "sentence_number": sentence_number,
                "sentence_index": sentence_index,
                "sentence_text": sentence_text,
                "images": sentence_images,
                "videos": videos.copy(),
                "visuals": (
                    sentence_visuals
                    or scene_visual_pool[:1]
                ),
                "sentence": sentence,
                "asset_scope": (
                    "sentence_match"
                    if sentence_images
                    and sentence_images[0].get(
                        "asset_scope"
                    ) == "sentence_match"
                    else "scene_fallback"
                ),
            }
        )

        print(
            f"SENTENCE {sentence_number}: "
            f"{len(sentence_images)} assigned image(s) "
            f"— "
            f"{sentence_images[0].get('asset_scope', 'none') "
            f"if sentence_images else 'none'}"
        )
"""

if old not in text:
    raise SystemExit(
        "ERROR: ORIGINAL SENTENCE RESULTS BLOCK NOT FOUND"
    )

path.write_text(
    text.replace(old, new, 1),
    encoding="utf-8",
)

print("SENTENCE VISUAL ASSIGNMENT PATCH: OK")