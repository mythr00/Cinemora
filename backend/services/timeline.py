import json
import re
from pathlib import Path


# ============================================================
# CONFIGURATION
# ============================================================

DEFAULT_DURATION = 10.0
MIN_SENTENCE_DURATION = 0.25


# ============================================================
# HELPERS
# ============================================================

def get_visual_path(visual):
    return (
        visual.get("file_path")
        or visual.get("path")
        or ""
    )


def visual_exists(visual):
    path = get_visual_path(visual)

    if not path:
        return False

    try:
        return Path(path).exists()
    except Exception:
        return False


def get_visual_type(visual):
    visual_type = (
        visual.get("type")
        or ""
    ).lower()

    if visual_type in {
        "video",
        "image",
    }:
        return visual_type

    path = get_visual_path(
        visual
    ).lower()

    if path.endswith(
        (
            ".mp4",
            ".mov",
            ".m4v",
            ".webm",
            ".mkv",
            ".avi",
        )
    ):
        return "video"

    return "image"


def normalize_text(text):
    if not text:
        return ""

    text = str(text).lower()

    text = re.sub(
        r"[^\w\s]",
        " ",
        text,
        flags=re.UNICODE,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def tokenize(text):
    normalized = normalize_text(text)

    if not normalized:
        return []

    return normalized.split()


# ============================================================
# CAPTION LOADING
# ============================================================

def load_caption_data(
    project_id,
    output_dir="media",
):
    project_dir = (
        Path(output_dir)
        / str(project_id)
    )

    caption_file = (
        project_dir
        / "captions.json"
    )

    if not caption_file.exists():
        raise FileNotFoundError(
            "Narration captions were not found: "
            f"{caption_file}"
        )

    with open(
        caption_file,
        "r",
        encoding="utf-8",
    ) as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise RuntimeError(
            "captions.json must contain an object."
        )

    segments = data.get(
        "segments",
        [],
    )

    if not isinstance(
        segments,
        list,
    ):
        raise RuntimeError(
            "captions.json segments must be a list."
        )

    valid_segments = []

    for segment in segments:

        if not isinstance(
            segment,
            dict,
        ):
            continue

        words = segment.get(
            "words",
            [],
        )

        if not isinstance(
            words,
            list,
        ):
            words = []

        try:
            start = float(
                segment.get(
                    "start",
                    0,
                )
            )

            end = float(
                segment.get(
                    "end",
                    start,
                )
            )

        except Exception:
            continue

        if end <= start:
            continue

        valid_segments.append({
            "start": start,
            "end": end,
            "text": segment.get(
                "text",
                "",
            ),
            "words": words,
        })

    duration = data.get(
        "duration"
    )

    if duration is None:

        if valid_segments:
            duration = max(
                segment["end"]
                for segment in valid_segments
            )
        else:
            duration = 0.0

    duration = float(
        duration
    )

    return {
        "audio_file":
            data.get(
                "audio_file",
                "",
            ),

        "language":
            data.get(
                "language",
                "",
            ),

        "duration":
            duration,

        "segments":
            valid_segments,
    }


# ============================================================
# SCRIPT SENTENCE EXTRACTION
# ============================================================

def get_scene_sentences(scene):
    sentences = scene.get(
        "sentences",
        [],
    )

    if isinstance(
        sentences,
        list,
    ):
        return [
            str(sentence)
            for sentence in sentences
            if str(sentence).strip()
        ]

    return []


# ============================================================
# NARRATION ALIGNMENT
# ============================================================



def align_sentences_to_segments(
    sentences,
    caption_segments,
    narration_duration,
):
    """
    Align script sentences to Whisper word timestamps.

    Whisper may:
    - combine multiple script sentences into one segment
    - split one script sentence across multiple segments
    - omit punctuation
    - slightly alter wording

    Therefore alignment is performed against the complete
    Whisper word sequence rather than assuming one segment
    equals one script sentence.
    """

    import difflib
    import re

    def normalize_token(value):
        value = str(value or "").lower()

        value = re.sub(
            r"[^a-z0-9']+",
            "",
            value,
        )

        return value

    # ---------------------------------------------------------
    # 1. Normalize script sentences
    # ---------------------------------------------------------

    normalized_sentences = []

    script_tokens = []

    for index, sentence in enumerate(sentences or []):

        if isinstance(sentence, dict):
            sentence_text = str(
                sentence.get("text", "")
            ).strip()
        else:
            sentence_text = str(sentence).strip()

        tokens = [
            normalize_token(token)
            for token in sentence_text.split()
        ]

        tokens = [
            token
            for token in tokens
            if token
        ]

        start_token = len(script_tokens)

        script_tokens.extend(tokens)

        end_token = len(script_tokens)

        normalized_sentences.append(
            {
                "index": index,
                "text": sentence_text,
                "tokens": tokens,
                "token_start": start_token,
                "token_end": end_token,
            }
        )

    # ---------------------------------------------------------
    # 2. Flatten Whisper words
    # ---------------------------------------------------------

    whisper_words = []

    for segment in caption_segments or []:

        for word in segment.get("words", []) or []:

            raw_word = str(
                word.get("word", "")
            ).strip()

            normalized = normalize_token(
                raw_word
            )

            if not normalized:
                continue

            start_time = float(
                word.get("start", 0.0)
            )

            end_time = float(
                word.get(
                    "end",
                    start_time,
                )
            )

            if end_time < start_time:
                end_time = start_time

            whisper_words.append(
                {
                    "text": raw_word,
                    "normalized": normalized,
                    "start": start_time,
                    "end": end_time,
                }
            )

    whisper_tokens = [
        word["normalized"]
        for word in whisper_words
    ]

    total_script_tokens = len(
        script_tokens
    )

    total_whisper_words = len(
        whisper_words
    )

    if not normalized_sentences:
        return []

    if not whisper_words:

        duration = float(
            narration_duration or 0.0
        )

        sentence_duration = (
            duration
            / len(normalized_sentences)
        )

        results = []

        for i, sentence in enumerate(
            normalized_sentences
        ):

            start_time = (
                i * sentence_duration
            )

            end_time = (
                (i + 1)
                * sentence_duration
            )

            results.append(
                {
                    "sentence_index": sentence["index"],
                    "text": sentence["text"],
                    "start": round(
                        start_time,
                        3,
                    ),
                    "end": round(
                        end_time,
                        3,
                    ),
                    "duration": round(
                        max(
                            0.0,
                            end_time - start_time,
                        ),
                        3,
                    ),
                    "word_start": 0,
                    "word_end": 0,
                    "alignment_score": 0.0,
                    "alignment_method": "uniform_fallback",
                }
            )

        return results

    # ---------------------------------------------------------
    # 3. Global token alignment
    # ---------------------------------------------------------

    matcher = difflib.SequenceMatcher(
        None,
        script_tokens,
        whisper_tokens,
        autojunk=False,
    )

    matching_blocks = matcher.get_matching_blocks()

    # Map each script token position to a Whisper word index.
    script_to_whisper = {}

    for block in matching_blocks:

        script_start = block.a
        whisper_start = block.b
        size = block.size

        for offset in range(size):

            script_index = (
                script_start + offset
            )

            whisper_index = (
                whisper_start + offset
            )

            script_to_whisper[
                script_index
            ] = whisper_index

    # ---------------------------------------------------------
    # 4. Interpolate unmatched script tokens
    # ---------------------------------------------------------

    matched_positions = sorted(
        script_to_whisper.keys()
    )

    def estimate_whisper_position(
        script_position,
    ):

        if not matched_positions:
            return int(
                (
                    script_position
                    / max(
                        1,
                        total_script_tokens,
                    )
                )
                * total_whisper_words
            )

        if script_position <= matched_positions[0]:

            return script_to_whisper[
                matched_positions[0]
            ]

        if script_position >= matched_positions[-1]:

            return min(
                total_whisper_words - 1,
                script_to_whisper[
                    matched_positions[-1]
                ]
                + (
                    script_position
                    - matched_positions[-1]
                ),
            )

        left = matched_positions[0]
        right = matched_positions[-1]

        for i in range(
            len(matched_positions) - 1
        ):

            current = matched_positions[i]
            following = matched_positions[i + 1]

            if (
                current
                <= script_position
                <= following
            ):

                left = current
                right = following
                break

        left_whisper = script_to_whisper[
            left
        ]

        right_whisper = script_to_whisper[
            right
        ]

        span_script = (
            right - left
        )

        if span_script <= 0:
            return left_whisper

        ratio = (
            script_position - left
        ) / span_script

        estimate = (
            left_whisper
            + ratio
            * (
                right_whisper
                - left_whisper
            )
        )

        return int(round(estimate))

    # ---------------------------------------------------------
    # 5. Build sentence timing windows
    # ---------------------------------------------------------

    results = []

    previous_end_time = 0.0

    for sentence_position, sentence in enumerate(
        normalized_sentences
    ):

        token_start = sentence[
            "token_start"
        ]

        token_end = sentence[
            "token_end"
        ]

        if token_end <= token_start:

            start_time = previous_end_time
            end_time = start_time

            results.append(
                {
                    "sentence_index": sentence["index"],
                    "text": sentence["text"],
                    "start": round(
                        start_time,
                        3,
                    ),
                    "end": round(
                        end_time,
                        3,
                    ),
                    "duration": 0.0,
                    "word_start": 0,
                    "word_end": 0,
                    "alignment_score": 0.0,
                    "alignment_method": "empty_sentence",
                }
            )

            continue

        mapped_indices = []

        for script_position in range(
            token_start,
            token_end,
        ):

            if script_position in script_to_whisper:

                mapped_indices.append(
                    script_to_whisper[
                        script_position
                    ]
                )

        # -----------------------------------------------------
        # Direct matched words
        # -----------------------------------------------------

        if mapped_indices:

            word_start = min(
                mapped_indices
            )

            word_end = max(
                mapped_indices
            ) + 1

        # -----------------------------------------------------
        # No direct match: estimate from neighboring
        # sentence boundaries.
        # -----------------------------------------------------

        else:

            estimated_start = (
                estimate_whisper_position(
                    token_start
                )
            )

            estimated_end = (
                estimate_whisper_position(
                    max(
                        token_start,
                        token_end - 1,
                    )
                )
                + 1
            )

            word_start = max(
                0,
                min(
                    total_whisper_words - 1,
                    estimated_start,
                ),
            )

            word_end = max(
                word_start + 1,
                min(
                    total_whisper_words,
                    estimated_end,
                ),
            )

        # -----------------------------------------------------
        # Clamp to chronological sequence
        # -----------------------------------------------------

        if results:

            minimum_word = results[-1][
                "word_end"
            ]

            if word_start < minimum_word:

                word_start = minimum_word

            if word_end <= word_start:

                word_end = min(
                    total_whisper_words,
                    word_start + 1,
                )

        if word_start >= total_whisper_words:

            word_start = max(
                0,
                total_whisper_words - 1,
            )

        if word_end > total_whisper_words:

            word_end = total_whisper_words

        selected_words = whisper_words[
            word_start:word_end
        ]

        # -----------------------------------------------------
        # Timestamp calculation
        # -----------------------------------------------------

        if selected_words:

            start_time = selected_words[0][
                "start"
            ]

            end_time = selected_words[-1][
                "end"
            ]

        else:

            start_time = previous_end_time
            end_time = previous_end_time

        if start_time < previous_end_time:

            start_time = previous_end_time

        if end_time < start_time:

            end_time = start_time

        # -----------------------------------------------------
        # Alignment score
        # -----------------------------------------------------

        expected = set(
            sentence["tokens"]
        )

        actual = set(
            word["normalized"]
            for word in selected_words
        )

        if expected:

            alignment_score = (
                len(
                    expected & actual
                )
                / len(expected)
            )

        else:

            alignment_score = 0.0

        results.append(
            {
                "sentence_index": sentence["index"],
                "text": sentence["text"],
                "start": round(
                    start_time,
                    3,
                ),
                "end": round(
                    end_time,
                    3,
                ),
                "duration": round(
                    max(
                        0.0,
                        end_time - start_time,
                    ),
                    3,
                ),
                "word_start": word_start,
                "word_end": word_end,
                "alignment_score": round(
                    alignment_score,
                    4,
                ),
                "alignment_method": "global_token_alignment",
            }
        )

        previous_end_time = end_time

    # ---------------------------------------------------------
    # 6. Final boundary
    # ---------------------------------------------------------

    narration_duration = float(
        narration_duration or 0.0
    )

    if results:

        results[-1]["end"] = round(
            narration_duration,
            3,
        )

        if (
            results[-1]["start"]
            > results[-1]["end"]
        ):

            results[-1]["start"] = results[-1][
                "end"
            ]

        results[-1]["duration"] = round(
            max(
                0.0,
                results[-1]["end"]
                - results[-1]["start"],
            ),
            3,
        )

    # ---------------------------------------------------------
    # 7. Final chronological safety
    # ---------------------------------------------------------

    for i in range(
        1,
        len(results),
    ):

        if (
            results[i]["start"]
            < results[i - 1]["end"]
        ):

            results[i]["start"] = results[
                i - 1
            ]["end"]

        if (
            results[i]["end"]
            < results[i]["start"]
        ):

            results[i]["end"] = results[
                i
            ]["start"]

        results[i]["duration"] = round(
            max(
                0.0,
                results[i]["end"]
                - results[i]["start"],
            ),
            3,
        )

    return results


def build_scene_timeline(
    scene_media,
    sentence_timings=None,
):

    scene_number = scene_media.get(
        "scene_number",
        1,
    )

    duration = float(
        scene_media.get(
            "duration_seconds",
            10,
        )
    )

    images = [
        image
        for image in scene_media.get(
            "images",
            [],
        )
        if visual_exists(image)
    ]

    videos = [
        video
        for video in scene_media.get(
            "videos",
            [],
        )
        if visual_exists(video)
    ]

    visuals = []

    for video in videos:
        visuals.append(video)

    for image in images:
        visuals.append(image)

    if not visuals:

        return {
            "scene_number":
                scene_number,

            "duration_seconds":
                duration,

            "items": [],

            "visuals": [],

            "video_sources": [],

            "sentence_timings":
                sentence_timings or [],
        }

    # --------------------------------------------------------
    # SENTENCE-LEVEL WINDOWS
    # --------------------------------------------------------

    if sentence_timings:

        items = []

        for sentence_index, timing in enumerate(
            sentence_timings
        ):

            start = float(
                timing["start"]
            )

            end = float(
                timing["end"]
            )

            # Extend the visual window through the beginning of
            # the next sentence so narration pauses are preserved.
            if sentence_index + 1 < len(sentence_timings):
                next_start = float(
                    sentence_timings[sentence_index + 1]["start"]
                )

                visual_end = max(
                    end,
                    next_start,
                )

            else:
                visual_end = max(
                    end,
                    duration,
                )

            sentence_duration = max(
                visual_end - start,
                MIN_SENTENCE_DURATION,
            )

            visual = visuals[
                sentence_index
                % len(visuals)
            ]

            visual_type = get_visual_type(
                visual
            )

            path = get_visual_path(
                visual
            )

            item = {
                "type":
                    visual_type,

                "index":
                    len(items),

                "scene_number":
                    scene_number,

                "sentence_index":
                    timing[
                        "sentence_index"
                    ],

                "sentence":
                    timing[
                        "text"
                    ],

                "start":
                    round(
                        start,
                        3,
                    ),

                "end":
                    round(
                        end,
                        3,
                    ),

                "duration":
                    round(
                        sentence_duration,
                        3,
                    ),

                "file_path":
                    path,

                "path":
                    path,

                "source_url":
                    visual.get(
                        "source_url",
                        "",
                    ),

                "thumbnail_url":
                    visual.get(
                        "thumbnail_url",
                        "",
                    ),

                "title":
                    visual.get(
                        "title",
                        "",
                    ),

                "source":
                    visual.get(
                        "source",
                        "",
                    ),

                "selection_score":
                    visual.get(
                        "selection_score",
                        80,
                    ),

                "alignment_score":
                    timing.get(
                        "alignment_score",
                        0,
                    ),

                "visual_relation":
                    "sentence_window",

                "motion":
                    "auto",

                "transition":
                    "cut",
            }

            if visual_type == "video":

                item["video_url"] = visual.get(
                    "video_url",
                    "",
                )

            else:

                item["image_url"] = visual.get(
                    "image_url",
                    "",
                )

                item["width"] = visual.get(
                    "width"
                )

                item["height"] = visual.get(
                    "height"
                )

            items.append(item)

        return {
            "scene_number":
                scene_number,

            "duration_seconds":
                duration,

            "items":
                items,

            "visuals":
                items,

            "video_sources": [
                {
                    "title":
                        video.get(
                            "title",
                            "",
                        ),

                    "video_url":
                        video.get(
                            "video_url",
                            "",
                        ),

                    "source":
                        video.get(
                            "source",
                            "",
                        ),

                    "snippet":
                        video.get(
                            "snippet",
                            "",
                        ),

                    "duration":
                        video.get(
                            "duration",
                            "",
                        ),

                    "thumbnail_url":
                        video.get(
                            "thumbnail_url",
                            "",
                        ),

                    "selection_score":
                        video.get(
                            "selection_score",
                            100,
                        ),
                }
                for video in videos
            ],

            "video_count":
                len(videos),

            "image_count":
                len(images),

            "sentence_timings":
                sentence_timings,
        }

    # --------------------------------------------------------
    # LEGACY FALLBACK
    # --------------------------------------------------------

    video_count = len(
        videos
    )

    image_count = len(
        images
    )

    if video_count > 0:

        video_weight = 1.5
        image_weight = 1.0

        total_weight = (
            video_count * video_weight
            + image_count * image_weight
        )

        video_duration = (
            duration
            * video_weight
            / total_weight
        )

        image_duration = (
            duration
            * image_weight
            / total_weight
        )

    else:

        video_duration = 0

        image_duration = (
            duration / image_count
            if image_count
            else duration
        )

    items = []

    current_time = 0.0

    for video in videos:

        remaining = (
            duration
            - current_time
        )

        if remaining <= 0:
            break

        item_duration = min(
            video_duration,
            remaining,
        )

        path = get_visual_path(
            video
        )

        items.append({
            "type": "video",
            "index": len(items),
            "scene_number": scene_number,
            "start": round(
                current_time,
                3,
            ),
            "end": round(
                current_time
                + item_duration,
                3,
            ),
            "duration": round(
                item_duration,
                3,
            ),
            "file_path": path,
            "path": path,
            "video_url": video.get(
                "video_url",
                "",
            ),
            "source_url": video.get(
                "source_url",
                "",
            ),
            "thumbnail_url": video.get(
                "thumbnail_url",
                "",
            ),
            "title": video.get(
                "title",
                "",
            ),
            "source": video.get(
                "source",
                "",
            ),
            "selection_score": video.get(
                "selection_score",
                100,
            ),
        })

        current_time += item_duration

    for image in images:

        remaining = (
            duration
            - current_time
        )

        if remaining <= 0:
            break

        item_duration = min(
            image_duration,
            remaining,
        )

        path = get_visual_path(
            image
        )

        items.append({
            "type": "image",
            "index": len(items),
            "scene_number": scene_number,
            "start": round(
                current_time,
                3,
            ),
            "end": round(
                current_time
                + item_duration,
                3,
            ),
            "duration": round(
                item_duration,
                3,
            ),
            "file_path": path,
            "path": path,
            "image_url": image.get(
                "image_url",
                "",
            ),
            "source_url": image.get(
                "source_url",
                "",
            ),
            "thumbnail_url": image.get(
                "thumbnail_url",
                "",
            ),
            "title": image.get(
                "title",
                "",
            ),
            "source": image.get(
                "source",
                "",
            ),
            "width": image.get(
                "width"
            ),
            "height": image.get(
                "height"
            ),
            "selection_score": image.get(
                "selection_score",
                80,
            ),
        })

        current_time += item_duration

    if items:

        difference = (
            duration
            - current_time
        )

        if abs(difference) > 0.001:

            items[-1]["duration"] += (
                difference
            )

            items[-1]["end"] = duration

    video_sources = [
        {
            "title":
                video.get(
                    "title",
                    "",
                ),

            "video_url":
                video.get(
                    "video_url",
                    "",
                ),

            "source":
                video.get(
                    "source",
                    "",
                ),

            "snippet":
                video.get(
                    "snippet",
                    "",
                ),

            "duration":
                video.get(
                    "duration",
                    "",
                ),

            "thumbnail_url":
                video.get(
                    "thumbnail_url",
                    "",
                ),

            "selection_score":
                video.get(
                    "selection_score",
                    100,
                ),
        }
        for video in videos
    ]

    return {
        "scene_number":
            scene_number,

        "duration_seconds":
            duration,

        "items":
            items,

        "visuals":
            items,

        "video_sources":
            video_sources,

        "video_count":
            len(videos),

        "image_count":
            len(images),

        "sentence_timings":
            [],
    }


# ============================================================
# BUILD PROJECT TIMELINE
# ============================================================

def build_project_timeline(
    project_id,
    processed_scenes,
    output_dir="media",
):

    project_dir = (
        Path(output_dir)
        / str(project_id)
    )

    project_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    timeline_file = (
        project_dir
        / "timeline.json"
    )

    # --------------------------------------------------------
    # LOAD NARRATION TIMINGS
    # --------------------------------------------------------

    caption_data = load_caption_data(
        project_id,
        output_dir,
    )

    narration_duration = float(
        caption_data[
            "duration"
        ]
    )

    caption_segments = caption_data[
        "segments"
    ]

    # --------------------------------------------------------
    # COLLECT PRODUCTION SENTENCES
    # --------------------------------------------------------

    all_sentences = []
    for scene_media in processed_scenes:

        scene = scene_media.get(
            "scene",
            {},
        )

        # Prefer the exact sentence records produced by media processing.
        sentences = scene_media.get(
            "sentences",
            [],
        )

        if not sentences:
            sentences = get_scene_sentences(
                scene
            )

        for sentence in sentences:

            if isinstance(sentence, dict):

                sentence_text = str(
                    sentence.get("sentence_text")
                    or sentence.get("text")
                    or sentence.get("sentence")
                    or ""
                ).strip()

            else:

                sentence_text = str(
                    sentence or ""
                ).strip()
            if sentence_text:

                all_sentences.append(
                    sentence_text
                )

    # --------------------------------------------------------
    # ALIGN SCRIPT TO NARRATION
    # --------------------------------------------------------

    sentence_timings = (
        align_sentences_to_segments(
            all_sentences,
            caption_segments,
            narration_duration,
        )
    )

    # --------------------------------------------------------
    # BUILD SCENE TIMELINES
    # --------------------------------------------------------

    project_time = 0.0

    all_items = []

    scene_results = []

    total_visuals = 0

    sentence_cursor = 0

    for scene_media in processed_scenes:

        scene = scene_media.get(
            "scene",
            {},
        )

        # Prefer the exact sentence records produced by media processing.
        scene_sentences = scene_media.get(
            "sentences",
            [],
        )

        if not scene_sentences:
            scene_sentences = get_scene_sentences(
                scene
            )

        scene_count = len(
            scene_sentences
        )

        scene_sentence_timings = (
            sentence_timings[
                sentence_cursor:
                sentence_cursor
                + scene_count
            ]
        )

        sentence_cursor += scene_count

        if scene_sentence_timings:

            scene_start = float(
                scene_sentence_timings[0][
                    "start"
                ]
            )

            scene_end = float(
                scene_sentence_timings[-1][
                    "end"
                ]
            )

            scene_duration = max(
                scene_end - scene_start,
                MIN_SENTENCE_DURATION,
            )

        else:

            scene_start = project_time

            scene_duration = float(
                scene_media.get(
                    "duration_seconds",
                    scene.get(
                        "duration_seconds",
                        DEFAULT_DURATION,
                    ),
                )
            )

            scene_end = (
                scene_start
                + scene_duration
            )

        scene_media_for_timeline = {
            **scene_media,

            "duration_seconds":
                scene_duration,
        }

        scene_result = (
            build_scene_timeline(
                scene_media_for_timeline,
                sentence_timings=
                    scene_sentence_timings,
            )
        )

        # ----------------------------------------------------
        # PROJECT TIMESTAMPS
        # ----------------------------------------------------

        for item in scene_result[
            "items"
        ]:

            item = {
                **item,

                "project_start":
                    round(
                        item["start"],
                        3,
                    ),

                "project_end":
                    round(
                        item["end"],
                        3,
                    ),
            }

            all_items.append(
                item
            )

        total_visuals += len(
            scene_result[
                "items"
            ]
        )

        scene_results.append({
            **scene_result,

            "start":
                round(
                    scene_start,
                    3,
                ),

            "end":
                round(
                    scene_end,
                    3,
                ),

            "duration_seconds":
                round(
                    scene_duration,
                    3,
                ),
        })

        project_time = max(
            project_time,
            scene_end,
        )

    # --------------------------------------------------------
    # FINAL DURATION MUST EQUAL NARRATION
    # --------------------------------------------------------

    final_duration = round(
        narration_duration,
        3,
    )

    # --------------------------------------------------------
    # TIMELINE RESULT
    # --------------------------------------------------------

    result = {
        "project_id":
            str(project_id),

        "scene_count":
            len(scene_results),

        "visual_count":
            total_visuals,

        "sentence_count":
            len(sentence_timings),

        "narration_duration_seconds":
            final_duration,

        "duration_seconds":
            final_duration,

        "scenes":
            scene_results,

        "items":
            all_items,

        "timing_source":
            "captions.json",

        "alignment":
            {
                "method":
                    "whisper_word_alignment",

                "whisper_segments":
                    len(
                        caption_segments
                    ),

                "script_sentences":
                    len(
                        all_sentences
                    ),

                "aligned_sentences":
                    len(
                        sentence_timings
                    ),
            },
    }

    with open(
        timeline_file,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            result,
            file,
            indent=2,
            ensure_ascii=False,
        )

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("TIMELINE COMPLETE")
    print("=" * 70)

    print(
        f"Scenes: "
        f"{result['scene_count']}"
    )

    print(
        f"Script sentences: "
        f"{result['sentence_count']}"
    )

    print(
        f"Whisper segments: "
        f"{len(caption_segments)}"
    )

    print(
        f"Visuals: "
        f"{result['visual_count']}"
    )

    print(
        f"Narration duration: "
        f"{final_duration} seconds"
    )

    if total_visuals:

        print(
            f"Average visual window: "
            f"{final_duration / total_visuals:.2f}s"
        )

    print(
        f"Saved: "
        f"{timeline_file}"
    )

    print("=" * 70)

    return {
        **result,

        "timeline_file":
            str(
                timeline_file
            ),

        "status":
            "timeline_created",
    }
