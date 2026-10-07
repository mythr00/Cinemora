import os
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from services.media import (
    search_images,
    search_videos,
    download_image,
)

DEFAULT_IMAGE_COUNT = 3
DEFAULT_VIDEO_COUNT = 2
DEFAULT_CLIP_DURATION = 8

MAX_IMAGE_RESULTS_PER_QUERY = 12
MAX_VIDEO_RESULTS_PER_QUERY = 8

# Number of search queries used to build the candidate pool.
MAX_SENTENCE_SEARCH_QUERIES = 8

# Maximum candidates retained before ranking.
MAX_CANDIDATES = 50

# ------------------------------------------------------------
# PROCESS SAFETY
# ------------------------------------------------------------

YT_DLP_TIMEOUT = 120
FFMPEG_TIMEOUT = 90
TRANSCRIPT_TIMEOUT = 60

# yt-dlp can leave these files behind when a download is
# interrupted or killed.
TEMP_FILE_SUFFIXES = (
    ".part",
    ".ytdl",
    ".temp",
)

TEMP_FILE_PATTERNS = (
    "*.part",
    "*.ytdl",
    "*.temp",
)


def kill_process_tree(process):
    """
    Kill a process and all child processes.

    Windows yt-dlp frequently launches ffmpeg as a child
    process. subprocess.run(timeout=...) only terminates the
    parent cleanly in some situations, leaving ffmpeg alive.

    taskkill /T /F is used on Windows so the complete tree
    disappears.
    """
    if process is None:
        return

    pid = getattr(process, "pid", None)

    if not pid:
        return

    try:
        subprocess.run(
            [
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(pid),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def run_command_safe(
    command,
    timeout,
    label,
):
    """
    Execute an external command with hard timeout protection.

    Returns:
        returncode, stdout, stderr

    On timeout, the complete Windows process tree is killed.
    """
    process = None

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            stdout, stderr = process.communicate(
                timeout=timeout
            )

        except subprocess.TimeoutExpired:
            print()
            print("=" * 70)
            print(f"{label} TIMED OUT")
            print(f"Timeout: {timeout}s")
            print("=" * 70)

            kill_process_tree(process)

            try:
                stdout, stderr = process.communicate(
                    timeout=5
                )
            except Exception:
                stdout = ""
                stderr = ""

            return (
                -999,
                stdout or "",
                (
                    stderr
                    or f"{label} timed out after {timeout}s"
                ),
            )

        return (
            process.returncode,
            stdout or "",
            stderr or "",
        )

    except Exception as error:
        print(
            f"{label} failed to start: {error}"
        )

        if process is not None:
            kill_process_tree(process)

        return (
            -998,
            "",
            str(error),
        )


def cleanup_temp_files(directory):
    """
    Remove interrupted yt-dlp/ffmpeg temporary files.
    """
    directory = Path(directory)

    if not directory.exists():
        return

    for pattern in TEMP_FILE_PATTERNS:
        for file in directory.glob(pattern):
            try:
                if file.is_file():
                    file.unlink()
                    print(
                        f"Removed temporary file: {file}"
                    )
            except OSError:
                pass


# ============================================================
# VALIDATION
# ============================================================

def validate_image(path):
    try:
        path = Path(path)

        if not path.exists():
            return False

        if path.stat().st_size < 1000:
            return False

        with Image.open(path) as img:
            img.verify()

        with Image.open(path) as img:
            img.load()

        return True

    except (UnidentifiedImageError, OSError, ValueError):
        return False


def validate_video(path):
    try:
        path = Path(path)

        if not path.exists():
            return False

        if path.stat().st_size < 10_000:
            return False

        return True

    except OSError:
        return False


# ============================================================
# IMAGE DOWNLOAD
# ============================================================

def download_valid_image(image_url, output_path):
    output_path = Path(output_path).resolve()

    try:
        if output_path.exists():
            output_path.unlink()

        download_image(
            image_url,
            str(output_path),
        )

        if not validate_image(output_path):
            try:
                output_path.unlink()
            except OSError:
                pass

            return False

        return True

    except Exception as error:
        print(
            f"IMAGE DOWNLOAD FAILED: {error}"
        )

        try:
            if output_path.exists():
                output_path.unlink()
        except OSError:
            pass

        return False


# ============================================================
# VIDEO DOWNLOAD
# ============================================================

def download_video_clip(
    video_url,
    output_path,
    start_time=0,
    duration=DEFAULT_CLIP_DURATION,
):
    """
    Download and normalize a short video clip.

    IMPORTANT:
    This function deliberately protects the worker from
    yt-dlp/ffmpeg processes that become stuck downloading
    enormous source files.

    A timeout kills the ENTIRE process tree, including ffmpeg
    children, and removes .part/.ytdl/.temp files.
    """
    output_path = Path(output_path)

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Never allow an old broken output to be mistaken for a
    # successful new download.
    try:
        if output_path.exists():
            output_path.unlink()
    except OSError:
        pass

    cleanup_temp_files(
        output_path.parent
    )

    output_template = str(
        output_path.with_name(
            output_path.stem + "_source.%(ext)s"
        )
    ).replace("\\", "/")

    section_end = start_time + duration

    print()
    print("=" * 70)
    print("yt-dlp extracting short video clip")
    print("=" * 70)
    print(video_url)
    print(f"Start: {start_time}s")
    print(f"Duration: {duration}s")
    print(f"Timeout: {YT_DLP_TIMEOUT}s")

    command = [
        sys.executable,
            "-m",
            "yt_dlp",

        "--no-playlist",
        "--no-warnings",
        "--restrict-filenames",

        # Do not retry a broken source for several minutes.
        "--retries",
        "1",

        "--fragment-retries",
        "1",

        "--socket-timeout",
        "15",

        # One fragment at a time makes stuck downloads easier
        # to terminate and avoids unnecessary bandwidth spikes.
        "--concurrent-fragments",
        "1",

        # Keep source resolution reasonable.
        "-f",
        "bv*[height<=720]+ba/b[height<=720]/best",

        # Extract only the requested section.
        "--download-sections",
        f"*{start_time}-{section_end}",

        # Required for accurate cuts on many sources.
        "--force-keyframes-at-cuts",

        "--merge-output-format",
        "mp4",

        "-o",
        output_template,

        video_url,
    ]

    returncode, stdout, stderr = run_command_safe(
        command,
        YT_DLP_TIMEOUT,
        "yt-dlp",
    )

    if returncode == -999:
        print(
            "yt-dlp was forcibly terminated."
        )

        cleanup_temp_files(
            output_path.parent
        )

        return False

    if returncode != 0:
        print(
            "yt-dlp clip extraction failed:"
        )
        print(
            stderr[-3000:]
        )

        cleanup_temp_files(
            output_path.parent
        )

        return False

    candidates = []

    for file in output_path.parent.glob(
        f"{output_path.stem}_source.*"
    ):
        if file.is_file():
            candidates.append(file)

    if not candidates:
        print(
            "No extracted video clip was found."
        )

        cleanup_temp_files(
            output_path.parent
        )

        return False

    # Ignore obvious temporary files.
    candidates = [
        file
        for file in candidates
        if file.suffix.lower()
        not in TEMP_FILE_SUFFIXES
    ]

    if not candidates:
        print(
            "Only temporary source files were found."
        )

        cleanup_temp_files(
            output_path.parent
        )

        return False

    candidates.sort(
        key=lambda p: -p.stat().st_size
    )

    source_clip = candidates[0]

    print(
        f"Downloaded source clip: {source_clip}"
    )

    # --------------------------------------------------------
    # FFmpeg normalization
    # --------------------------------------------------------

    ffmpeg_command = [
        "ffmpeg",
        "-y",

        "-i",
        str(source_clip),

        "-t",
        str(duration),

        "-vf",
        (
            "scale=1920:1080:"
            "force_original_aspect_ratio=increase,"
            "crop=1920:1080"
        ),

        "-r",
        "30",

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "23",

        "-c:a",
        "aac",

        "-movflags",
        "+faststart",

        str(output_path),
    ]

    print(
        f"Running FFmpeg with {FFMPEG_TIMEOUT}s timeout..."
    )

    returncode, stdout, stderr = run_command_safe(
        ffmpeg_command,
        FFMPEG_TIMEOUT,
        "FFmpeg",
    )

    if returncode == -999:
        print(
            "FFmpeg was forcibly terminated."
        )

        try:
            if output_path.exists():
                output_path.unlink()
        except OSError:
            pass

        try:
            if source_clip.exists():
                source_clip.unlink()
        except OSError:
            pass

        cleanup_temp_files(
            output_path.parent
        )

        return False

    if returncode != 0:
        print(
            "FFmpeg clip conversion failed:"
        )
        print(
            stderr[-3000:]
        )

        try:
            if output_path.exists():
                output_path.unlink()
        except OSError:
            pass

        try:
            if source_clip.exists():
                source_clip.unlink()
        except OSError:
            pass

        cleanup_temp_files(
            output_path.parent
        )

        return False

    # --------------------------------------------------------
    # Cleanup source
    # --------------------------------------------------------

    try:
        if source_clip.exists():
            source_clip.unlink()
    except OSError:
        pass

    cleanup_temp_files(
        output_path.parent
    )

    # --------------------------------------------------------
    # Final validation
    # --------------------------------------------------------

    if not validate_video(
        output_path
    ):
        print(
            "Final video clip failed validation."
        )

        try:
            output_path.unlink()
        except OSError:
            pass

        cleanup_temp_files(
            output_path.parent
        )

        return False

    print(
        f"VALID VIDEO CLIP: {output_path}"
    )

    return True


def download_video_with_ytdlp(
    video_url,
    output_path,
    start_time=0,
    duration=DEFAULT_CLIP_DURATION,
):
    return download_video_clip(
        video_url,
        output_path,
        start_time,
        duration,
    )


# ============================================================
# TEXT / QUERY HELPERS
# ============================================================

def clean_query(query):
    if not query:
        return ""

    return " ".join(
        str(query).strip().split()
    )


def get_sentence_list(scene):
    sentences = (
        scene.get("sentences")
        or scene.get("sentence_analysis")
        or scene.get("sentence_data")
        or []
    )

    if not isinstance(sentences, list):
        return []

    return sentences


def get_sentence_text(sentence):
    if isinstance(sentence, str):
        return sentence.strip()

    if not isinstance(sentence, dict):
        return ""

    return (
        sentence.get("text")
        or sentence.get("sentence")
        or sentence.get("content")
        or ""
    ).strip()


# ============================================================
# SENTENCE ID
# ============================================================

def get_sentence_number(sentence, fallback):
    if isinstance(sentence, dict):
        value = (
            sentence.get("sentence_id")
            or sentence.get("sentence_number")
            or sentence.get("number")
            or sentence.get("index")
        )

        if isinstance(value, int):
            return value

    return fallback


# ============================================================
# SENTENCE ANALYSIS HELPERS
# ============================================================

def get_sentence_queries(sentence):
    if not isinstance(sentence, dict):
        return []

    possible_keys = [
        "search_queries",
        "queries",
        "visual_queries",
    ]

    for key in possible_keys:
        value = sentence.get(key)

        if isinstance(value, list):
            queries = []

            for item in value:
                query = clean_query(item)

                if query and query not in queries:
                    queries.append(query)

            if queries:
                return queries

    single_query = (
        sentence.get("search_query")
        or sentence.get("visual_query")
        or sentence.get("query")
        or ""
    )

    single_query = clean_query(
        single_query
    )

    if single_query:
        return [single_query]

    return []


def get_sentence_entities(sentence):
    if not isinstance(sentence, dict):
        return []

    entities = sentence.get(
        "entities",
        [],
    )

    if not isinstance(entities, list):
        return []

    return [
        str(entity).strip()
        for entity in entities
        if str(entity).strip()
    ]


def get_sentence_actions(sentence):
    if not isinstance(sentence, dict):
        return []

    actions = sentence.get(
        "actions",
        [],
    )

    if not isinstance(actions, list):
        return []

    return [
        str(action).strip()
        for action in actions
        if str(action).strip()
    ]


def get_sentence_visual_types(sentence):
    if not isinstance(sentence, dict):
        return []

    values = sentence.get(
        "visual_types",
        [],
    )

    if not isinstance(values, list):
        return []

    return [
        str(value).strip()
        for value in values
        if str(value).strip()
    ]


def get_sentence_event(sentence):
    if not isinstance(sentence, dict):
        return ""

    event = (
        sentence.get("event")
        or ""
    )

    return str(event).strip()


def get_visual_intent(sentence):
    if not isinstance(sentence, dict):
        return {}

    intent = sentence.get(
        "visual_intent",
        {},
    )

    if not isinstance(intent, dict):
        return {}

    return intent


def get_preferred_visuals(sentence):
    intent = get_visual_intent(
        sentence
    )

    preferred = intent.get(
        "preferred_visuals",
        [],
    )

    if not isinstance(preferred, list):
        return []

    return [
        clean_query(item)
        for item in preferred
        if clean_query(item)
    ]


# ============================================================
# RESULT TEXT
# ============================================================

def result_text(result):
    if not isinstance(result, dict):
        return ""

    parts = [
        result.get("title", ""),
        result.get("snippet", ""),
        result.get("source", ""),
        result.get("url", ""),
        result.get("link", ""),
        result.get("video_url", ""),
        result.get("image_url", ""),
    ]

    return " ".join(
        str(part)
        for part in parts
        if part
    ).lower()


def result_title(result):
    if not isinstance(result, dict):
        return ""

    return str(
        result.get("title", "")
        or ""
    ).strip()


def result_source(result):
    if not isinstance(result, dict):
        return ""

    return str(
        result.get("source", "")
        or ""
    ).strip().lower()


def result_url(result):
    if not isinstance(result, dict):
        return ""

    return str(
        result.get("url")
        or result.get("link")
        or result.get("video_url")
        or result.get("image_url")
        or ""
    ).strip()


# ============================================================
# BAD / LOW QUALITY CONTENT
# ============================================================

BAD_TITLE_TERMS = [
    "playlist",
    "compilation",
    "top 10",
    "top ten",
    "reaction",
    "reacts",
    "meme",
    "memes",
    "shorts",
    "tiktok",
    "funny",
    "comedy",
    "wallpaper",
    "stock photo",
    "stock footage",
    "ai generated",
    "ai-generated",
    "fan edit",
    "edit compilation",
]


GENERIC_VIDEO_TERMS = [
    "best moments",
    "top moments",
    "funniest",
    "reaction",
    "reacts to",
    "podcast clips",
    "short video",
    "viral video",
]


def is_bad_result(result):
    text = result_text(result)

    if not text:
        return True

    for term in BAD_TITLE_TERMS:
        if term in text:
            return True

    title = result_title(
        result
    ).lower()

    for term in GENERIC_VIDEO_TERMS:
        if term in title:
            return True

    return False


# ============================================================
# SOURCE QUALITY
# ============================================================

HIGH_QUALITY_SOURCES = [
    "reuters",
    "associated press",
    "ap news",
    "bbc",
    "cnbc",
    "bloomberg",
    "financial times",
    "wall street journal",
    "wsj",
    "new york times",
    "washington post",
    "guardian",
    "abc news",
    "nbc news",
    "cbs news",
    "cnn",
    "pbs",
    "npr",
    "national geographic",
    "smithsonian",
    "national archives",
    "library of congress",
    "government",
    "gov",
    "sec.gov",
    "court",
    "archive.org",
    "wikimedia",
]


GOOD_DOCUMENTARY_SOURCES = [
    "documentary",
    "history",
    "pbs",
    "bbc",
    "cnbc",
    "bloomberg",
    "wsj",
    "national geographic",
    "smithsonian",
    "archive",
]


LOW_QUALITY_SOURCES = [
    "pinterest",
    "reddit",
    "quora",
    "fandom",
    "blogspot",
]


def source_quality_score(result):
    text = result_text(
        result
    )

    source = result_source(
        result
    )

    score = 0

    for domain in HIGH_QUALITY_SOURCES:
        if domain in text or domain in source:
            score += 10

    for term in GOOD_DOCUMENTARY_SOURCES:
        if term in text or term in source:
            score += 5

    for term in LOW_QUALITY_SOURCES:
        if term in text or term in source:
            score -= 8

    # YouTube can contain excellent documentary footage,
    # but we don't automatically give it the same weight
    # as a major news/archive source.
    if "youtube" in text:
        score += 3

    if "instagram" in text:
        score -= 2

    if "facebook" in text:
        score -= 1

    return score


# ============================================================
# TOKEN HELPERS
# ============================================================

def normalize_token(value):
    value = str(
        value or ""
    ).lower()

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value,
    )

    return value.strip()


def tokenize(value):
    normalized = normalize_token(
        value
    )

    if not normalized:
        return set()

    return {
        token
        for token in normalized.split()
        if len(token) >= 3
    }


# ============================================================
# SEMANTIC RESULT SCORING
# ============================================================

def score_entity_match(
    result,
    entities,
):
    text = result_text(
        result
    )

    score = 0

    for entity in entities:

        entity_normalized = normalize_token(
            entity
        )

        if not entity_normalized:
            continue

        if entity_normalized in text:
            score += 18

        # Also reward all important words in
        # multi-word entities.
        entity_tokens = tokenize(
            entity
        )

        if entity_tokens:

            result_tokens = tokenize(
                text
            )

            matched = (
                entity_tokens
                & result_tokens
            )

            score += len(
                matched
            ) * 4

    return score


def score_action_match(
    result,
    actions,
):
    text = result_text(
        result
    )

    score = 0

    action_terms = {
        "withdrawal": [
            "withdraw",
            "withdrawal",
            "withdrawals",
            "customers withdrawing",
        ],

        "filing": [
            "filed",
            "filing",
            "file",
        ],

        "bankruptcy": [
            "bankruptcy",
            "bankrupt",
            "chapter 11",
        ],

        "collapse": [
            "collapse",
            "collapsed",
        ],

        "acquisition": [
            "acquired",
            "acquisition",
            "bought",
        ],

        "sale": [
            "sold",
            "sale",
            "selling",
        ],

        "arrest": [
            "arrest",
            "arrested",
        ],

        "death": [
            "killed",
            "killing",
            "died",
            "death",
        ],
    }

    for action in actions:

        terms = action_terms.get(
            action,
            [action],
        )

        for term in terms:

            if term in text:
                score += 12

    return score


def score_event_match(
    result,
    event,
):
    if not event:
        return 0

    text = result_text(
        result
    )

    event_terms = {
        "bankruptcy": [
            "bankruptcy",
            "bankrupt",
            "chapter 11",
            "bankruptcy filing",
            "bankruptcy court",
        ],

        "customer_withdrawals": [
            "withdrawal",
            "withdrawals",
            "withdrawing",
            "customer money",
            "customers",
        ],

        "collapse": [
            "collapse",
            "collapsed",
            "collapse explained",
        ],

        "court": [
            "court",
            "judge",
            "hearing",
            "lawsuit",
        ],

        "arrest": [
            "arrest",
            "arrested",
            "custody",
        ],
    }

    terms = event_terms.get(
        event,
        [event],
    )

    score = 0

    for term in terms:

        if term in text:
            score += 16

    return score


def score_visual_type_match(
    result,
    visual_types,
):
    text = result_text(
        result
    )

    type_terms = {
        "company": [
            "company",
            "headquarters",
            "office",
            "corporate",
            "business",
            "exchange",
        ],

        "finance": [
            "finance",
            "financial",
            "crypto",
            "cryptocurrency",
            "trading",
            "exchange",
            "money",
            "market",
        ],

        "legal": [
            "court",
            "legal",
            "lawsuit",
            "bankruptcy",
            "filing",
            "judge",
            "chapter 11",
        ],

        "event": [
            "event",
            "announcement",
            "collapse",
            "crisis",
        ],

        "person": [
            "ceo",
            "founder",
            "president",
            "person",
        ],

        "technology": [
            "technology",
            "computer",
            "software",
            "website",
            "platform",
            "digital",
        ],

        "military": [
            "military",
            "army",
            "navy",
            "missile",
            "soldier",
        ],
    }

    score = 0

    for visual_type in visual_types:

        terms = type_terms.get(
            visual_type,
            [visual_type],
        )

        for term in terms:

            if term in text:
                score += 5

    return score


def score_preferred_visuals(
    result,
    preferred_visuals,
):
    if not preferred_visuals:
        return 0

    text = result_text(
        result
    )

    title = result_title(
        result
    ).lower()

    score = 0

    for preferred in preferred_visuals:

        preferred_tokens = tokenize(
            preferred
        )

        if not preferred_tokens:
            continue

        result_tokens = tokenize(
            text
        )

        overlap = (
            preferred_tokens
            & result_tokens
        )

        if overlap:

            score += (
                len(overlap) * 7
            )

        # Stronger bonus when the actual title
        # directly resembles the desired visual.
        preferred_normalized = normalize_token(
            preferred
        )

        title_normalized = normalize_token(
            title
        )

        if (
            preferred_normalized
            and preferred_normalized
            in title_normalized
        ):
            score += 20

    return score


def score_query_match(
    result,
    query,
):
    result_tokens = tokenize(
        result_text(result)
    )

    query_tokens = tokenize(
        query
    )

    if not result_tokens or not query_tokens:
        return 0

    overlap = (
        query_tokens
        & result_tokens
    )

    return len(overlap) * 4


def score_sentence_keywords(
    result,
    sentence,
):
    if not isinstance(
        sentence,
        dict,
    ):
        return 0

    keywords = sentence.get(
        "keywords",
        [],
    )

    if not isinstance(
        keywords,
        list,
    ):
        return 0

    result_tokens = tokenize(
        result_text(result)
    )

    score = 0

    for keyword in keywords:

        keyword_tokens = tokenize(
            keyword
        )

        if not keyword_tokens:
            continue

        overlap = (
            keyword_tokens
            & result_tokens
        )

        score += (
            len(overlap) * 2
        )

    return score


def score_generic_penalty(
    result,
    sentence,
):
    text = result_text(
        result
    )

    title = result_title(
        result
    ).lower()

    penalty = 0

    generic_terms = [
        "explained",
        "what is",
        "everything you need to know",
        "guide",
        "tutorial",
        "podcast",
        "interview",
        "reaction",
        "commentary",
        "opinion",
    ]

    for term in generic_terms:

        if term in title:
            penalty += 2

    # Generic FTX footage should not beat event-specific
    # footage when the sentence is about an event.
    event = get_sentence_event(
        sentence
    )

    if event == "bankruptcy":

        generic_terms = [
            "headquarters",
            "office",
            "logo",
            "trading",
            "founder",
        ]

        for term in generic_terms:

            if term in text:
                penalty += 12

    if event == "customer_withdrawals":

        generic_terms = [
            "headquarters",
            "office",
            "logo",
            "founder",
        ]

        for term in generic_terms:

            if term in text:
                penalty += 10

    return penalty


def calculate_result_score(
    result,
    query,
    sentence,
):
    """
    Editorial visual ranking.

    The result must not merely contain the same keywords.
    It should visually represent what the narrator is saying.
    """

    if is_bad_result(result):
        return -999

    entities = get_sentence_entities(sentence)
    actions = get_sentence_actions(sentence)
    visual_types = get_sentence_visual_types(sentence)
    event = get_sentence_event(sentence).lower()
    preferred_visuals = get_preferred_visuals(sentence)

    sentence_text = get_sentence_text(sentence).lower()
    text = result_text(result)
    title = result_title(result).lower()
    url = result_url(result).lower()

    score = 0

    # ========================================================
    # 1. ENTITY MATCH
    # ========================================================

    for entity in entities:
        entity_text = normalize_token(entity)

        if not entity_text:
            continue

        if entity_text in text:
            score += 30

        entity_tokens = tokenize(entity)
        result_tokens = tokenize(text)

        matched = entity_tokens & result_tokens
        score += len(matched) * 5

    # ========================================================
    # 2. ACTION MATCH
    # ========================================================

    action_terms = {
        "withdrawal": [
            "withdraw",
            "withdrawal",
            "withdrawals",
            "withdrawing",
            "customer funds",
            "customer money",
        ],
        "filing": [
            "filed",
            "filing",
            "court filing",
            "petition",
        ],
        "bankruptcy": [
            "bankruptcy",
            "bankrupt",
            "chapter 11",
        ],
        "collapse": [
            "collapse",
            "collapsed",
            "crash",
            "fall",
        ],
        "acquisition": [
            "acquisition",
            "acquired",
            "buyout",
        ],
        "sale": [
            "sale",
            "sold",
            "selling",
        ],
        "arrest": [
            "arrest",
            "arrested",
            "detained",
            "custody",
        ],
        "death": [
            "death",
            "died",
            "killed",
        ],
    }

    for action in actions:
        terms = action_terms.get(
            action.lower(),
            [action.lower()],
        )

        for term in terms:
            if term in text:
                score += 25

    # ========================================================
    # 3. EVENT MATCH
    # ========================================================

    event_terms = {
        "bankruptcy": [
            "bankruptcy",
            "chapter 11",
            "bankruptcy filing",
            "bankruptcy court",
            "court filing",
        ],
        "customer_withdrawals": [
            "withdrawal",
            "withdrawals",
            "withdrawing",
            "customer money",
            "customer funds",
        ],
        "collapse": [
            "collapse",
            "collapsed",
            "crash",
        ],
        "court": [
            "court",
            "judge",
            "hearing",
            "lawsuit",
        ],
        "arrest": [
            "arrest",
            "arrested",
            "custody",
        ],
    }

    for term in event_terms.get(
        event,
        [event] if event else [],
    ):
        if term in text:
            score += 30

    # ========================================================
    # 4. VISUAL INTENT
    # ========================================================

    visual_intent = sentence.get(
        "visual_intent",
        {},
    )

    if isinstance(visual_intent, dict):
        intent_type = str(
            visual_intent.get("type")
            or visual_intent.get("category")
            or visual_intent.get("intent")
            or ""
        ).lower()
    else:
        intent_type = str(
            visual_intent or ""
        ).lower()

    # Direct visual categories
    if intent_type in {
        "company",
        "organization",
        "business",
        "fact",
    }:
        for term in [
            "headquarters",
            "office",
            "company",
            "corporate",
            "exchange",
            "building",
            "logo",
        ]:
            if term in text:
                score += 20

    if intent_type in {
        "event",
        "historical_event",
        "incident",
        "crisis",
    }:
        for term in [
            "event",
            "crisis",
            "incident",
            "collapse",
            "announcement",
        ]:
            if term in text:
                score += 25

    if intent_type in {
        "legal",
        "legal_event",
        "bankruptcy",
        "court",
    }:
        for term in [
            "bankruptcy",
            "chapter 11",
            "court",
            "filing",
            "petition",
            "judge",
            "hearing",
            "legal",
            "document",
        ]:
            if term in text:
                score += 35

    if intent_type in {
        "person",
        "people",
        "individual",
    }:
        for term in [
            "portrait",
            "photo",
            "interview",
            "ceo",
            "founder",
        ]:
            if term in text:
                score += 25

    if intent_type in {
        "interface",
        "website",
        "platform",
        "screen",
        "technology",
        "object",
    }:
        for term in [
            "screen",
            "interface",
            "website",
            "platform",
            "dashboard",
            "exchange",
            "app",
            "software",
            "trading",
        ]:
            if term in text:
                score += 35

    # ========================================================
    # 5. PREFERRED VISUALS
    # ========================================================

    result_tokens = tokenize(text)

    for preferred in preferred_visuals:
        preferred_tokens = tokenize(preferred)

        if not preferred_tokens:
            continue

        overlap = preferred_tokens & result_tokens

        score += len(overlap) * 10

        preferred_normalized = normalize_token(preferred)
        title_normalized = normalize_token(title)

        if (
            preferred_normalized
            and preferred_normalized in title_normalized
        ):
            score += 40

    # ========================================================
    # 6. QUERY MATCH
    # ========================================================

    query_tokens = tokenize(query)

    if query_tokens:
        overlap = query_tokens & result_tokens
        score += len(overlap) * 5

    # ========================================================
    # 7. SENTENCE KEYWORDS
    # ========================================================

    sentence_tokens = tokenize(sentence_text)

    important_stopwords = {
        "this",
        "that",
        "they",
        "their",
        "there",
        "which",
        "where",
        "when",
        "what",
        "were",
        "once",
        "eventually",
    }

    for word in sentence_tokens:
        if word in important_stopwords:
            continue

        if word in title:
            score += 6
        elif word in text:
            score += 2

    # ========================================================
    # 8. SOURCE QUALITY
    # ========================================================

    score += source_quality_score(result)

    # ========================================================
    # 9. GENERIC CONTENT PENALTIES
    # ========================================================

    generic_terms = [
        "top 10",
        "top ten",
        "greatest history",
        "history videos",
        "preview",
        "reaction",
        "reacts",
        "podcast",
        "commentary",
        "opinion",
        "guide",
        "tutorial",
        "everything you need to know",
    ]

    for term in generic_terms:
        if term in title:
            score -= 25

    # Social media is weaker for documentary sourcing.
    for domain in [
        "twitter.com",
        "x.com",
        "facebook.com",
        "instagram.com",
        "tiktok.com",
    ]:
        if domain in url:
            score -= 30

    # ========================================================
    # 10. EVENT-SPECIFIC PENALTIES
    # ========================================================

    if event == "bankruptcy":
        for term in [
            "headquarters",
            "office",
            "logo",
            "trading",
            "founder",
        ]:
            if term in text and not any(
                x in text
                for x in [
                    "bankruptcy",
                    "chapter 11",
                    "court",
                    "filing",
                ]
            ):
                score -= 25

    if event == "customer_withdrawals":
        for term in [
            "headquarters",
            "office",
            "logo",
        ]:
            if term in text:
                score -= 20

    # ========================================================
    # 11. STRONG FTX EVENT OVERRIDES
    # ========================================================

    if "ftx" in sentence_text:

        if "withdraw" in sentence_text:
            if any(
                term in text
                for term in [
                    "withdrawal",
                    "withdrawing",
                    "customer funds",
                    "customer money",
                ]
            ):
                score += 60

        if "bankruptcy" in sentence_text:
            if any(
                term in text
                for term in [
                    "bankruptcy",
                    "chapter 11",
                    "court filing",
                    "bankruptcy filing",
                ]
            ):
                score += 70

    return score


def rank_results(
    results,
    query,
    sentence=None,
):
    ranked = []

    for result in results:

        if not isinstance(
            result,
            dict,
        ):
            continue

        score = calculate_result_score(
            result,
            query,
            sentence,
        )

        if score <= -900:
            continue

        ranked.append(
            (
                score,
                result,
            )
        )

    ranked.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    return ranked


# ============================================================
# CANDIDATE COLLECTION
# ============================================================

def collect_video_candidates(
    sentence,
    queries,
):
    candidates = []
    seen_urls = set()

    search_queries = queries[
        :MAX_SENTENCE_SEARCH_QUERIES
    ]

    print()
    print(
        f"Building video candidate pool "
        f"from {len(search_queries)} queries..."
    )

    for query in search_queries:

        print(
            f"VIDEO QUERY: {query}"
        )

        try:
            results = search_videos(
                query,
                limit=MAX_VIDEO_RESULTS_PER_QUERY,
            )

        except Exception as error:
            print(
                f"Video search failed: {error}"
            )
            continue

        for result in results:

            if not isinstance(
                result,
                dict,
            ):
                continue

            if is_bad_result(result):
                continue

            url = result_url(
                result
            )

            if not url:
                continue

            if url in seen_urls:
                continue

            seen_urls.add(url)

            score = calculate_result_score(
                result,
                query,
                sentence,
            )

            result_copy = dict(
                result
            )

            result_copy[
                "_asset_score"
            ] = score

            result_copy[
                "_search_query"
            ] = query

            candidates.append(
                result_copy
            )

            if len(candidates) >= MAX_CANDIDATES:
                break

        if len(candidates) >= MAX_CANDIDATES:
            break

    candidates.sort(
        key=lambda result: result.get(
            "_asset_score",
            0,
        ),
        reverse=True,
    )

    return candidates


def collect_image_candidates(
    sentence,
    queries,
):
    candidates = []
    seen_urls = set()

    search_queries = queries[
        :MAX_SENTENCE_SEARCH_QUERIES
    ]

    print()
    print(
        f"Building image candidate pool "
        f"from {len(search_queries)} queries..."
    )

    for query in search_queries:

        print(
            f"IMAGE QUERY: {query}"
        )

        try:
            results = search_images(
                query,
                MAX_IMAGE_RESULTS_PER_QUERY,
            )

        except Exception as error:
            print(
                f"Image search failed: {error}"
            )
            continue

        for result in results:

            if not isinstance(
                result,
                dict,
            ):
                continue

            if is_bad_result(result):
                continue

            url = result_url(
                result
            )

            if not url:
                continue

            if url in seen_urls:
                continue

            seen_urls.add(url)

            score = calculate_result_score(
                result,
                query,
                sentence,
            )

            result_copy = dict(
                result
            )

            result_copy[
                "_asset_score"
            ] = score

            result_copy[
                "_search_query"
            ] = query

            candidates.append(
                result_copy
            )

            if len(candidates) >= MAX_CANDIDATES:
                break

        if len(candidates) >= MAX_CANDIDATES:
            break

    candidates.sort(
        key=lambda result: result.get(
            "_asset_score",
            0,
        ),
        reverse=True,
    )

    return candidates






# ============================================================
# ASSET QUALITY GATE
# ============================================================

BAD_VISUAL_TERMS = {
    "meme",
    "memes",
    "funny",
    "reaction",
    "thumbnail",
    "stock photo",
    "stock image",
    "wallpaper",
    "fan art",
    "fanart",
    "reddit",
    "pinterest",
    "instagram",
    "tiktok",
    "x.com",
    "twitter",
    "clickbait",
    "ai generated",
    "generated image",
}

LOW_QUALITY_DOMAINS = {
    "pinterest.com",
    "reddit.com",
    "x.com",
    "twitter.com",
    "instagram.com",
    "tiktok.com",
}

STRONG_DOCUMENTARY_DOMAINS = {
    "reuters.com",
    "apnews.com",
    "bbc.com",
    "bbc.co.uk",
    "nytimes.com",
    "washingtonpost.com",
    "theguardian.com",
    "bloomberg.com",
    "cnbc.com",
    "ft.com",
    "wsj.com",
    "archives.gov",
    "loc.gov",
    "wikimedia.org",
    "wikipedia.org",
    "sec.gov",
    "courtlistener.com",
}

LEGAL_VISUAL_TERMS = {
    "bankruptcy",
    "chapter 11",
    "court",
    "filing",
    "filed",
    "complaint",
    "lawsuit",
    "indictment",
    "document",
    "documents",
    "press release",
}

NEWS_VISUAL_TERMS = {
    "news",
    "breaking",
    "report",
    "reports",
    "exclusive",
    "investigation",
    "coverage",
    "collapse",
}

REAL_FOOTAGE_TERMS = {
    "footage",
    "archive",
    "archival",
    "video",
    "live",
    "press conference",
    "interview",
    "hearing",
    "speech",
    "announcement",
    "security footage",
    "surveillance",
}

PERSON_VISUAL_TERMS = {
    "photo",
    "portrait",
    "pictured",
    "interview",
    "appearance",
    "press conference",
}

def get_candidate_text(candidate):
    """
    Combine all useful candidate metadata into one searchable string.
    """
    if not isinstance(candidate, dict):
        return ""

    values = [
        candidate.get("title", ""),
        candidate.get("snippet", ""),
        candidate.get("description", ""),
        candidate.get("source", ""),
        candidate.get("domain", ""),
        candidate.get("url", ""),
        candidate.get("link", ""),
    ]

    return " ".join(
        str(value or "")
        for value in values
    ).lower()


def get_candidate_domain(candidate):
    """
    Extract a normalized domain.
    """
    if not isinstance(candidate, dict):
        return ""

    domain = str(
        candidate.get("domain", "")
        or ""
    ).lower().strip()

    domain = domain.replace(
        "https://",
        "",
    )

    domain = domain.replace(
        "http://",
        "",
    )

    domain = domain.split("/")[0]

    if domain.startswith("www."):
        domain = domain[4:]

    return domain


def calculate_asset_quality(
    candidate,
    sentence,
):
    """
    Calculate editorial quality independently from
    the existing relevance score.

    Returns:
        {
            "score": int,
            "decision": "accept/reject",
            "reasons": [...]
        }
    """

    text = get_candidate_text(
        candidate
    )

    domain = get_candidate_domain(
        candidate
    )

    sentence_text = get_sentence_text(
        sentence
    ).lower()

    entities = [
        str(x).lower()
        for x in get_sentence_entities(
            sentence
        )
    ]

    actions = [
        str(x).lower()
        for x in get_sentence_actions(
            sentence
        )
    ]

    event = str(
        get_sentence_event(
            sentence
        )
        or ""
    ).lower()

    score = 50
    reasons = []

    # --------------------------------------------------------
    # Entity match
    # --------------------------------------------------------

    entity_matches = 0

    for entity in entities:
        if entity and entity in text:
            entity_matches += 1

    if entity_matches:
        bonus = min(
            25,
            entity_matches * 12,
        )

        score += bonus

        reasons.append(
            f"entity_match:+{bonus}"
        )

    else:
        score -= 15

        reasons.append(
            "missing_entity:-15"
        )

    # --------------------------------------------------------
    # Event match
    # --------------------------------------------------------

    event_terms = set()

    if event == "bankruptcy":
        event_terms.update(
            LEGAL_VISUAL_TERMS
        )

    elif event == "customer_withdrawals":
        event_terms.update({
            "withdrawal",
            "withdrawals",
            "customers",
            "customer funds",
            "money",
            "exchange",
        })

    elif event == "historical_event":
        event_terms.update(
            NEWS_VISUAL_TERMS
        )

    elif event:
        event_terms.add(event)

    event_matches = sum(
        1
        for term in event_terms
        if term in text
    )

    if event_matches:
        bonus = min(
            25,
            event_matches * 6,
        )

        score += bonus

        reasons.append(
            f"event_match:+{bonus}"
        )

    # --------------------------------------------------------
    # Action match
    # --------------------------------------------------------

    action_map = {
        "withdrawal": {
            "withdrawal",
            "withdrawals",
            "withdraw",
            "withdrew",
            "customers",
        },
        "bankruptcy": {
            "bankruptcy",
            "chapter 11",
            "insolvency",
        },
        "filing": {
            "filing",
            "filed",
            "court",
            "documents",
        },
        "collapse": {
            "collapse",
            "collapsed",
            "crisis",
        },
    }

    action_matches = 0

    for action in actions:
        terms = action_map.get(
            action,
            {action},
        )

        if any(
            term in text
            for term in terms
        ):
            action_matches += 1

    if action_matches:
        bonus = min(
            15,
            action_matches * 7,
        )

        score += bonus

        reasons.append(
            f"action_match:+{bonus}"
        )

    # --------------------------------------------------------
    # Strong documentary source
    # --------------------------------------------------------

    if domain in STRONG_DOCUMENTARY_DOMAINS:
        score += 15

        reasons.append(
            "trusted_source:+15"
        )

    # --------------------------------------------------------
    # Bad content
    # --------------------------------------------------------

    bad_matches = [
        term
        for term in BAD_VISUAL_TERMS
        if term in text
    ]

    if bad_matches:
        penalty = min(
            45,
            len(bad_matches) * 15,
        )

        score -= penalty

        reasons.append(
            f"bad_content:-{penalty}"
        )

    # --------------------------------------------------------
    # Low-quality social sources
    # --------------------------------------------------------

    if domain in LOW_QUALITY_DOMAINS:
        score -= 30

        reasons.append(
            "low_quality_domain:-30"
        )

    # --------------------------------------------------------
    # Legal material gets a special boost
    # when sentence is about bankruptcy/legal events.
    # --------------------------------------------------------

    if event == "bankruptcy":
        legal_matches = sum(
            1
            for term in LEGAL_VISUAL_TERMS
            if term in text
        )

        if legal_matches:
            bonus = min(
                20,
                legal_matches * 4,
            )

            score += bonus

            reasons.append(
                f"legal_visual:+{bonus}"
            )

    # --------------------------------------------------------
    # Real footage boost
    # --------------------------------------------------------

    footage_matches = sum(
        1
        for term in REAL_FOOTAGE_TERMS
        if term in text
    )

    if footage_matches:
        bonus = min(
            12,
            footage_matches * 4,
        )

        score += bonus

        reasons.append(
            f"real_footage:+{bonus}"
        )

    # --------------------------------------------------------
    # Direct sentence phrase overlap
    # --------------------------------------------------------

    sentence_tokens = set(
        tokenize(sentence_text)
    )

    candidate_tokens = set(
        tokenize(text)
    )

    overlap = (
        sentence_tokens
        & candidate_tokens
    )

    meaningful_overlap = {
        token
        for token in overlap
        if len(token) >= 5
    }

    if meaningful_overlap:
        bonus = min(
            15,
            len(meaningful_overlap) * 3,
        )

        score += bonus

        reasons.append(
            f"phrase_overlap:+{bonus}"
        )

    score = max(
        0,
        min(
            100,
            score,
        ),
    )

    # Reject only genuinely weak assets.
    decision = (
        "accept"
        if score >= 45
        else "reject"
    )

    return {
        "score": score,
        "decision": decision,
        "reasons": reasons,
    }


def apply_asset_quality_gate(
    candidates,
    sentence,
):
    """
    Filter and reorder candidates using editorial quality.
    """

    evaluated = []

    for candidate in candidates:
        quality = calculate_asset_quality(
            candidate,
            sentence,
        )

        enriched = dict(
            candidate
        )

        enriched[
            "quality_score"
        ] = quality["score"]

        enriched[
            "quality_decision"
        ] = quality["decision"]

        enriched[
            "quality_reasons"
        ] = quality["reasons"]

        evaluated.append(
            enriched
        )

    evaluated.sort(
        key=lambda item: (
            item.get(
                "quality_score",
                0,
            ),
            item.get(
                "score",
                0,
            ),
        ),
        reverse=True,
    )

    return [
        candidate
        for candidate in evaluated
        if candidate.get(
            "quality_decision"
        ) == "accept"
    ]


def print_quality_report(
    candidates,
    sentence,
):
    """
    Print the editorial decision for debugging.
    """

    print()
    print(
        "============================================================"
    )
    print(
        "ASSET QUALITY GATE"
    )
    print(
        "============================================================"
    )

    for index, candidate in enumerate(
        candidates[:5],
        start=1,
    ):
        quality = calculate_asset_quality(
            candidate,
            sentence,
        )

        title = candidate.get(
            "title",
            "Untitled",
        )

        print()
        print(
            f"{index}. {title[:100]}"
        )

        print(
            f"   Quality: {quality['score']}/100"
        )

        print(
            f"   Decision: {quality['decision'].upper()}"
        )

        if quality["reasons"]:
            print(
                "   Reasons: "
                + ", ".join(
                    quality["reasons"]
                )
            )

    print(
        "============================================================"
    )



# ============================================================
# RELEVANT VIDEO MOMENT EXTRACTION
# ============================================================

def parse_vtt_timestamp(value):
    """
    Convert VTT timestamp to seconds.
    Supports:
        HH:MM:SS.mmm
        MM:SS.mmm
    """
    value = value.strip()

    parts = value.split(":")

    try:
        if len(parts) == 3:
            hours = float(parts[0])
            minutes = float(parts[1])
            seconds = float(parts[2])
            return hours * 3600 + minutes * 60 + seconds

        if len(parts) == 2:
            minutes = float(parts[0])
            seconds = float(parts[1])
            return minutes * 60 + seconds

    except ValueError:
        return None

    return None


def clean_transcript_text(value):
    """
    Clean subtitle markup while preserving useful words.
    """
    value = str(value or "")

    value = re.sub(
        r"<[^>]+>",
        " ",
        value,
    )

    value = re.sub(
        r"\[[^\]]+\]",
        " ",
        value,
    )

    value = re.sub(
        r"\([^\)]+\)",
        " ",
        value,
    )

    value = value.replace(
        "&amp;",
        "and",
    )

    value = value.replace(
        "&quot;",
        '"',
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def parse_vtt_file(vtt_path):
    """
    Parse a WebVTT subtitle file into timestamped cues.
    """
    path = Path(vtt_path)

    if not path.exists():
        return []

    try:
        content = path.read_text(
            encoding="utf-8",
            errors="ignore",
        )
    except Exception:
        return []

    lines = content.splitlines()

    cues = []

    current_start = None
    current_end = None
    current_text = []

    def flush():
        nonlocal current_start
        nonlocal current_end
        nonlocal current_text

        if (
            current_start is not None
            and current_end is not None
            and current_text
        ):
            joined = clean_transcript_text(
                " ".join(current_text)
            )

            if joined:
                cues.append(
                    {
                        "start": current_start,
                        "end": current_end,
                        "text": joined,
                    }
                )

        current_start = None
        current_end = None
        current_text = []

    for line in lines:
        line = line.strip()

        if not line:
            flush()
            continue

        if "-->" in line:
            flush()

            timestamp_parts = line.split("-->")

            if len(timestamp_parts) >= 2:
                start = timestamp_parts[0].strip()
                end = timestamp_parts[1].strip()

                start_seconds = parse_vtt_timestamp(
                    start
                )

                end_seconds = parse_vtt_timestamp(
                    end
                )

                if (
                    start_seconds is not None
                    and end_seconds is not None
                ):
                    current_start = start_seconds
                    current_end = end_seconds

            continue

        if (
            line.startswith("WEBVTT")
            or line.startswith("Kind:")
            or line.startswith("Language:")
            or re.fullmatch(r"\d+", line)
        ):
            continue

        if current_start is not None:
            current_text.append(line)

    flush()

    return cues


def get_video_transcript(
    video_url,
    temp_dir,
):
    """
    Extract English subtitles/transcript safely.

    Like video downloading, this is protected against yt-dlp
    hanging indefinitely.
    """
    temp_dir = Path(temp_dir)

    temp_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Remove leftovers from a previous attempt.
    for file in temp_dir.glob("*"):
        try:
            if file.is_file():
                file.unlink()
        except OSError:
            pass

    output_template = str(
        temp_dir / "transcript.%(ext)s"
    )

    command = [
        sys.executable,
            "-m",
            "yt_dlp",

        "--no-playlist",
        "--no-warnings",
        "--skip-download",

        "--retries",
        "1",

        "--fragment-retries",
        "1",

        "--socket-timeout",
        "15",

        "--write-auto-subs",
        "--write-subs",

        "--sub-langs",
        "en,en-US,en-GB",

        "--sub-format",
        "vtt",

        "-o",
        output_template,

        video_url,
    ]

    print(
        "Extracting video transcript..."
    )

    returncode, stdout, stderr = run_command_safe(
        command,
        TRANSCRIPT_TIMEOUT,
        "Transcript yt-dlp",
    )

    if returncode == -999:
        print(
            "Transcript extraction timed out."
        )

        for file in temp_dir.glob("*"):
            try:
                if file.is_file():
                    file.unlink()
            except OSError:
                pass

        return []

    if returncode != 0:
        print(
            "Transcript extraction failed:"
        )

        if stderr:
            print(
                stderr[-2000:]
            )

        for file in temp_dir.glob("*"):
            try:
                if file.is_file():
                    file.unlink()
            except OSError:
                pass

        return []

    files = list(
        temp_dir.glob(
            "transcript*.vtt"
        )
    )

    if not files:
        print(
            "No transcript/subtitles available."
        )
        return []

    files.sort(
        key=lambda p: -p.stat().st_size
    )

    transcript = parse_vtt_file(
        files[0]
    )

    # Always clean transcript files.
    for file in files:
        try:
            file.unlink()
        except OSError:
            pass

    if not transcript:
        print(
            "Transcript was empty."
        )
        return []

    print(
        f"Transcript cues: {len(transcript)}"
    )

    return transcript


def build_transcript_search_terms(
    sentence,
):
    """
    Build strong search terms from the sentence.
    """
    terms = []

    sentence_text = get_sentence_text(
        sentence
    )

    entities = get_sentence_entities(
        sentence
    )

    actions = get_sentence_actions(
        sentence
    )

    event = get_sentence_event(
        sentence
    )

    for value in entities:
        value = normalize_token(value)

        if value:
            terms.append(value)

    action_terms = {
        "withdrawal": [
            "withdrawal",
            "withdraw",
            "customers",
            "customer funds",
        ],
        "bankruptcy": [
            "bankruptcy",
            "chapter 11",
            "filed",
        ],
        "filing": [
            "filing",
            "filed",
            "court",
        ],
        "collapse": [
            "collapse",
            "collapsed",
        ],
        "acquisition": [
            "acquired",
            "acquisition",
        ],
        "arrest": [
            "arrested",
            "arrest",
        ],
        "death": [
            "death",
            "died",
            "killed",
        ],
    }

    for action in actions:
        for term in action_terms.get(
            action.lower(),
            [action],
        ):
            normalized = normalize_token(
                term
            )

            if normalized:
                terms.append(normalized)

    if event:
        for token in tokenize(event):
            terms.append(token)

    # Add important words from the sentence.
    stopwords = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "was",
        "were",
        "is",
        "are",
        "to",
        "of",
        "in",
        "on",
        "for",
        "with",
        "their",
        "they",
        "that",
        "this",
        "eventually",
        "once",
        "one",
        "from",
    }

    for token in tokenize(sentence_text):
        if token in stopwords:
            continue

        if len(token) >= 4:
            terms.append(token)

    # Deduplicate while preserving order.
    final_terms = []

    for term in terms:
        if term and term not in final_terms:
            final_terms.append(term)

    return final_terms


def score_transcript_cue(
    cue,
    search_terms,
):
    """
    Score transcript relevance to the current sentence.

    Important:
    A transcript about a generic topic such as bankruptcy
    must not beat an FTX-specific transcript merely because
    they share generic keywords.
    """

    text = str(
        cue.get("text", "")
    ).lower().strip()

    if not text:
        return 0

    normalized_text = (
        text
        .replace(",", " ")
        .replace(".", " ")
        .replace("!", " ")
        .replace("?", " ")
        .replace(":", " ")
        .replace(";", " ")
        .replace("-", " ")
    )

    words = set(
        normalized_text.split()
    )

    # --------------------------------------------------------
    # Existing pipeline uses a list of search terms.
    # --------------------------------------------------------

    if isinstance(search_terms, dict):
        terms = []

        for key in (
            "phrases",
            "entities",
            "actions",
            "events",
            "keywords",
        ):
            terms.extend(
                search_terms.get(
                    key,
                    [],
                )
            )
    else:
        terms = search_terms or []

    clean_terms = []

    for term in terms:
        term = str(
            term
        ).lower().strip()

        if term and term not in clean_terms:
            clean_terms.append(term)

    # --------------------------------------------------------
    # Detect important entity terms.
    # --------------------------------------------------------

    entity_terms = [
        term
        for term in clean_terms
        if (
            len(term) >= 3
            and term not in {
                "the",
                "company",
                "customers",
                "customer",
                "money",
                "bankruptcy",
                "filing",
                "court",
                "exchange",
                "crypto",
                "cryptocurrency",
            }
        )
    ]

    # FTX is a particularly important entity for the
    # current documentary pipeline.
    requires_ftx = (
        "ftx" in clean_terms
    )

    has_ftx = (
        "ftx" in words
        or "ftx" in text
    )

    score = 0
    matched_terms = 0

    # --------------------------------------------------------
    # Exact and multi-word matches.
    # --------------------------------------------------------

    for term in clean_terms:

        if " " in term:

            term_words = set(
                term.split()
            )

            if term in text:
                score += 35
                matched_terms += 1

            elif term_words.issubset(words):
                score += 20
                matched_terms += 1

        else:

            if term in words:
                score += 7
                matched_terms += 1

    # --------------------------------------------------------
    # Multiple matching terms reinforce one another.
    # --------------------------------------------------------

    if matched_terms >= 2:
        score += 15

    if matched_terms >= 3:
        score += 15

    if matched_terms >= 4:
        score += 10

    # --------------------------------------------------------
    # CRITICAL ENTITY GATE
    #
    # If the sentence is explicitly about FTX but the
    # transcript never mentions FTX, do not allow generic
    # bankruptcy/business content to become a strong match.
    # --------------------------------------------------------

    if requires_ftx and not has_ftx:

        print(
            "Transcript rejected by entity gate: "
            "FTX not mentioned."
        )

        return min(
            score,
            20,
        )

    # --------------------------------------------------------
    # Reward explicit entity presence.
    # --------------------------------------------------------

    if has_ftx:
        score += 20

    # --------------------------------------------------------
    # Very weak single-term matches should not win.
    # --------------------------------------------------------

    if matched_terms == 1:
        score = min(
            score,
            20,
        )

    return score


def find_relevant_video_moment(
    video_url,
    sentence,
    temp_dir,
):
    """
    Find the best timestamp in a video's transcript
    for the current sentence.

    Returns structured evidence so the caller can decide
    whether this video actually contains a useful match.
    """

    transcript = get_video_transcript(
        video_url,
        temp_dir,
    )

    if not transcript:
        print(
            "No transcript available."
        )

        return {
            "clip_start": 0,
            "score": 0,
            "matched_text": "",
            "transcript_available": False,
        }

    search_terms = build_transcript_search_terms(
        sentence
    )

    if not search_terms:
        print(
            "No transcript search terms."
        )

        return {
            "clip_start": 0,
            "score": 0,
            "matched_text": "",
            "transcript_available": True,
        }

    best_score = 0
    best_start = 0
    best_text = ""

    # --------------------------------------------------------
    # Score individual transcript cues.
    # --------------------------------------------------------

    for cue in transcript:
        score = score_transcript_cue(
            cue,
            search_terms,
        )

        if score > best_score:
            best_score = score
            best_start = cue.get(
                "start",
                0,
            )
            best_text = cue.get(
                "text",
                "",
            )

    # --------------------------------------------------------
    # Also score short windows of consecutive cues.
    # This catches cases where the relevant sentence
    # is spread across several subtitle lines.
    # --------------------------------------------------------

    for index in range(
        len(transcript)
    ):
        window = transcript[
            index:index + 4
        ]

        if not window:
            continue

        combined_text = " ".join(
            cue.get("text", "")
            for cue in window
        )

        combined = {
            "text": combined_text
        }

        score = score_transcript_cue(
            combined,
            search_terms,
        )

        if score > best_score:
            best_score = score
            best_start = window[0].get(
                "start",
                0,
            )
            best_text = combined_text

    # --------------------------------------------------------
    # Require a meaningful transcript match.
    # --------------------------------------------------------

    if best_score < 8:
        print(
            "Transcript exists, but no strong "
            "transcript match was found."
        )

        return {
            "clip_start": 0,
            "score": best_score,
            "matched_text": best_text,
            "transcript_available": True,
        }

    # Give the edit a small lead-in so the clip
    # does not begin exactly when the speaker starts.
    clip_start = max(
        0,
        float(best_start) - 2.0,
    )

    print()
    print(
        "=============================================="
    )
    print(
        "RELEVANT VIDEO MOMENT FOUND"
    )
    print(
        "=============================================="
    )
    print(
        f"Transcript score: {best_score}"
    )
    print(
        f"Timestamp: {clip_start:.2f}s"
    )
    print(
        f"Matched text: {best_text[:300]}"
    )
    print(
        "=============================================="
    )

    return {
        "clip_start": clip_start,
        "score": best_score,
        "matched_text": best_text,
        "transcript_available": True,
    }


# ============================================================
# SENTENCE VIDEO SEARCH
# ============================================================

def process_sentence_videos(
    sentence,
    sentence_dir,
    video_count=DEFAULT_VIDEO_COUNT,
):
    os.makedirs(
        sentence_dir,
        exist_ok=True,
    )

    sentence_text = get_sentence_text(
        sentence
    )

    sentence_number = get_sentence_number(
        sentence,
        1,
    )

    queries = get_sentence_queries(
        sentence
    )

    if not queries:
        print(
            "No sentence video queries."
        )
        return []

    print()
    print("-" * 70)
    print(
        f"SENTENCE {sentence_number} VIDEO SEARCH"
    )
    print(
        f"Text: {sentence_text}"
    )
    print("-" * 70)

    print(
        f"Event: {get_sentence_event(sentence)}"
    )

    print(
        f"Entities: "
        f"{get_sentence_entities(sentence)}"
    )

    print(
        f"Actions: "
        f"{get_sentence_actions(sentence)}"
    )

    print(
        f"Preferred visuals: "
        f"{get_preferred_visuals(sentence)}"
    )

    candidates = collect_video_candidates(
        sentence,
        queries,
    )

    if not candidates:
        print(
            "No usable video candidates found."
        )
        return []

    print()
    print(
        "TOP VIDEO CANDIDATES:"
    )

    for rank, candidate in enumerate(
        candidates[:5],
        start=1,
    ):
        print(
            f"{rank}. "
            f"[score={candidate.get('_asset_score', 0)}] "
            f"{candidate.get('title', '')}"
        )

    # ========================================================
    # ASSET QUALITY GATE
    # ========================================================

    print()
    print(
        "=" * 70
    )
    print(
        "VIDEO ASSET QUALITY GATE"
    )
    print(
        "=" * 70
    )

    quality_candidates = []

    for candidate in candidates:

        quality = calculate_asset_quality(
            candidate,
            sentence,
        )

        candidate["quality_score"] = (
            quality["score"]
        )

        candidate["quality_decision"] = (
            quality["decision"]
        )

        candidate["quality_reasons"] = (
            quality["reasons"]
        )

        print()
        print(
            f"TITLE: "
            f"{candidate.get('title', '')[:100]}"
        )

        print(
            f"Search score: "
            f"{candidate.get('_asset_score', 0)}"
        )

        print(
            f"Quality score: "
            f"{quality['score']}/100"
        )

        print(
            f"Decision: "
            f"{quality['decision'].upper()}"
        )

        if quality["reasons"]:
            print(
                "Reasons: "
                + ", ".join(
                    quality["reasons"]
                )
            )

        if quality["decision"] == "accept":
            quality_candidates.append(
                candidate
            )

    # Sort by editorial quality first,
    # then original search relevance.
    quality_candidates.sort(
        key=lambda item: (
            item.get(
                "quality_score",
                0,
            ),
            item.get(
                "_asset_score",
                0,
            ),
        ),
        reverse=True,
    )

    print()
    print(
        f"ACCEPTED VIDEO CANDIDATES: "
        f"{len(quality_candidates)}"
    )

    # --------------------------------------------------------
    # TRANSCRIPT CANDIDATE POOL
    #
    # The quality gate can accept many near-duplicate results.
    # Transcript extraction is expensive, so only the strongest
    # candidates are inspected for an exact spoken moment.
    # --------------------------------------------------------

    # Transcript extraction is expensive. Only inspect the
    # strongest quality candidates first.
    transcript_candidates = quality_candidates[:12]

    print()
    print(
        f"TRANSCRIPT CANDIDATE POOL: "
        f"{len(transcript_candidates)}"
    )

    # If every candidate is rejected, preserve the
    # original search candidates as a final fallback.
    if not quality_candidates:
        print(
            "Quality gate rejected all candidates."
        )
        print(
            "Using original search ranking as fallback."
        )

        quality_candidates = candidates

    # ========================================================
    # VIDEO EXTRACTION
    # ========================================================

    videos = []

    attempted = 0

    candidate_pool = (
        transcript_candidates
        if transcript_candidates
        else candidates
    )

    for candidate in candidate_pool:

        if len(videos) >= video_count:
            break

        attempted += 1

        video_url = (
            candidate.get("video_url")
            or candidate.get("url")
            or candidate.get("link")
            or ""
        ).strip()

        if not video_url:
            print(
                "Skipping candidate with no video URL."
            )
            continue

        index = len(videos) + 1

        output_path = os.path.join(
            sentence_dir,
            f"video_{index}.mp4",
        )

        title = candidate.get(
            "title",
            "",
        )

        score = candidate.get(
            "_asset_score",
            0,
        )

        quality_score = candidate.get(
            "quality_score",
            0,
        )

        query = candidate.get(
            "_search_query",
            "",
        )

        print()
        print(
            "=" * 70
        )
        print(
            f"VIDEO CANDIDATE "
            f"{attempted}/{len(quality_candidates)}"
        )
        print(
            "=" * 70
        )

        print(
            f"Search score: {score}"
        )

        print(
            f"Quality score: {quality_score}/100"
        )

        print(
            f"Title: {title}"
        )

        print(
            f"Query: {query}"
        )

        print(
            f"Source: "
            f"{candidate.get('source', '')}"
        )

        # ----------------------------------------------------
        # Find the actual relevant moment.
        # ----------------------------------------------------

        transcript_dir = os.path.join(
            sentence_dir,
            f"_transcript_{attempted}",
        )

        # Remove stale transcript files from a previous
        # attempt before checking this candidate.
        transcript_path = Path(
            transcript_dir
        )

        transcript_path.mkdir(
            parents=True,
            exist_ok=True,
        )

        for old_file in transcript_path.glob(
            "transcript*.vtt"
        ):
            try:
                old_file.unlink()
            except OSError:
                pass

        moment = find_relevant_video_moment(
            video_url=video_url,
            sentence=sentence,
            temp_dir=transcript_dir,
        )

        clip_start = moment.get(
            "clip_start",
            0,
        )

        transcript_score = moment.get(
            "score",
            0,
        )

        matched_text = moment.get(
            "matched_text",
            "",
        )

        transcript_available = moment.get(
            "transcript_available",
            False,
        )

        candidate[
            "_transcript_available"
        ] = transcript_available

        candidate[
            "_transcript_score"
        ] = transcript_score

        candidate[
            "_matched_text"
        ] = matched_text

        candidate[
            "_clip_start"
        ] = clip_start

        print(
            f"Transcript available: "
            f"{transcript_available}"
        )

        print(
            f"Transcript match score: "
            f"{transcript_score}"
        )

        if matched_text:
            print(
                f"Matched transcript: "
                f"{matched_text[:250]}"
            )

        # ----------------------------------------------------
        # TRANSCRIPT-AWARE SELECTION
        #
        # Strong transcript matches are preferred because
        # they prove that the actual spoken content of the
        # source video relates to the current sentence.
        #
        # 50+ is treated as a meaningful match.
        # ----------------------------------------------------

        if (
            transcript_available
            and transcript_score >= 50
        ):
            print()
            print(
                "TRANSCRIPT-BACKED CANDIDATE ACCEPTED"
            )

            print(
                f"Transcript score: "
                f"{transcript_score}"
            )

            print(
                f"Clip timestamp: "
                f"{clip_start:.2f}s"
            )

        elif transcript_available:
            print(
                f"WEAK TRANSCRIPT MATCH "
                f"({transcript_score})"
            )

            print(
                "Continuing to next ranked candidate..."
            )

            if "fallback_video_candidate" not in locals():
                fallback_video_candidate = {
                    "candidate": candidate,
                    "video_url": video_url,
                    "title": title,
                    "score": score,
                    "quality_score": quality_score,
                    "query": query,
                }

            continue

        else:
            print(
                "NO TRANSCRIPT — candidate will not "
                "be selected yet."
            )

            print(
                "Continuing to next ranked candidate..."
            )

            if "fallback_video_candidate" not in locals():
                fallback_video_candidate = {
                    "candidate": candidate,
                    "video_url": video_url,
                    "title": title,
                    "score": score,
                    "quality_score": quality_score,
                    "query": query,
                }

            continue

        # ----------------------------------------------------
        # Download the actual clip.
        # ----------------------------------------------------

        success = download_video_clip(
            video_url,
            output_path,
            clip_start,
            DEFAULT_CLIP_DURATION,
        )

        if not success:
            print(
                "VIDEO DOWNLOAD FAILED."
            )

            # Remove failed output if one was created.
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except OSError:
                pass

            continue

        if not os.path.exists(output_path):
            print(
                "VIDEO FILE WAS NOT CREATED."
            )
            continue

        file_size = os.path.getsize(
            output_path
        )

        if file_size < 5000:
            print(
                "VIDEO FILE TOO SMALL — REJECTED."
            )

            try:
                os.remove(output_path)
            except OSError:
                pass

            continue

        # ----------------------------------------------------
        # Build metadata.
        # ----------------------------------------------------

        video_asset = {
            "type": "video",
            "path": output_path,
            "url": video_url,
            "title": title,
            "source": candidate.get(
                "source",
                "",
            ),
            "query": query,
            "score": score,
            "quality_score": quality_score,
            "quality_reasons": candidate.get(
                "quality_reasons",
                [],
            ),
            "clip_start": clip_start,
            "clip_duration": (
                DEFAULT_CLIP_DURATION
            ),
            "transcript_available": (
                transcript_available
            ),
        }

        videos.append(
            video_asset
        )

        print()
        print(
            "VALID VIDEO CLIP:"
            f" {output_path}"
        )

        print(
            f"Clip start: "
            f"{clip_start:.2f}s"
        )

        print(
            f"Clip duration: "
            f"{DEFAULT_CLIP_DURATION}s"
        )

        # ----------------------------------------------------
        # Once we have enough usable assets, stop.
        # ----------------------------------------------------

        if len(videos) >= video_count:
            break

    # ========================================================
    # FALLBACK
    #
    # If no candidate had a transcript, use the best-quality
    # transcript-less candidate rather than returning zero
    # videos.
    # ========================================================

    if (
        not videos
        and "fallback_video_candidate" in locals()
        and len(videos) < video_count
    ):
        fallback = fallback_video_candidate

        print()
        print(
            "=" * 70
        )
        print(
            "NO TRANSCRIPT-BACKED VIDEO FOUND"
        )
        print(
            "USING BEST QUALITY FALLBACK"
        )
        print(
            "=" * 70
        )

        fallback_url = fallback[
            "video_url"
        ]

        fallback_output = os.path.join(
            sentence_dir,
            "video_1.mp4",
        )

        print(
            f"Fallback title: "
            f"{fallback['title']}"
        )

        print(
            f"Fallback quality: "
            f"{fallback['quality_score']}/100"
        )

        success = download_video_clip(
            fallback_url,
            fallback_output,
            0,
            DEFAULT_CLIP_DURATION,
        )

        if (
            success
            and os.path.exists(fallback_output)
            and os.path.getsize(fallback_output) >= 5000
        ):
            videos.append(
                {
                    "type": "video",
                    "path": fallback_output,
                    "url": fallback_url,
                    "title": fallback["title"],
                    "source": fallback[
                        "candidate"
                    ].get(
                        "source",
                        "",
                    ),
                    "query": fallback["query"],
                    "score": fallback["score"],
                    "quality_score": fallback[
                        "quality_score"
                    ],
                    "quality_reasons": fallback[
                        "candidate"
                    ].get(
                        "quality_reasons",
                        [],
                    ),
                    "clip_start": 0,
                    "clip_duration": (
                        DEFAULT_CLIP_DURATION
                    ),
                    "transcript_available": False,
                }
            )

            print(
                f"FALLBACK VIDEO CREATED: "
                f"{fallback_output}"
            )

    print()
    print(
        f"VIDEO ASSETS SELECTED: "
        f"{len(videos)}/{video_count}"
    )

    return videos


# ============================================================
# SENTENCE IMAGE SEARCH
# ============================================================

def process_sentence_images(
    sentence,
    sentence_dir,
    image_count=DEFAULT_IMAGE_COUNT,
):
    os.makedirs(
        sentence_dir,
        exist_ok=True,
    )

    sentence_text = get_sentence_text(
        sentence
    )

    sentence_number = get_sentence_number(
        sentence,
        1,
    )

    queries = get_sentence_queries(
        sentence
    )

    if not queries:
        print(
            "No sentence image queries."
        )
        return []

    print()
    print("-" * 70)
    print(
        f"SENTENCE {sentence_number} IMAGE SEARCH"
    )
    print(
        f"Text: {sentence_text}"
    )
    print("-" * 70)

    candidates = collect_image_candidates(
        sentence,
        queries,
    )

    if not candidates:
        print(
            "No usable image candidates found."
        )
        return []

    print()
    print(
        "TOP IMAGE CANDIDATES:"
    )

    for rank, candidate in enumerate(
        candidates[:5],
        start=1,
    ):
        print(
            f"{rank}. "
            f"[score={candidate.get('_asset_score', 0)}] "
            f"{candidate.get('title', '')}"
        )

    images = []

    for candidate in candidates:

        if len(images) >= target_image_count:
            break

        image_url = (
            candidate.get("image_url")
            or candidate.get("url")
            or candidate.get("link")
            or ""
        ).strip()

        if not image_url:
            continue

        index = len(images) + 1

        output_path = os.path.join(
            sentence_dir,
            f"image_{index}.jpg",
        )

        title = candidate.get(
            "title",
            "",
        )

        score = candidate.get(
            "_asset_score",
            0,
        )

        query = candidate.get(
            "_search_query",
            "",
        )

        print()
        print(
            f"IMAGE CANDIDATE "
            f"{index}/{image_count}"
        )

        print(
            f"Score: {score}"
        )

        print(
            f"Title: {title}"
        )

        print(
            f"Query: {query}"
        )

        if not download_valid_image(
            image_url,
            output_path,
        ):
            print(
                "Image unavailable. "
                "Trying next ranked candidate..."
            )
            continue

        try:
            with Image.open(
                output_path
            ) as img:
                width, height = img.size

        except Exception:

            try:
                os.remove(
                    output_path
                )
            except OSError:
                pass

            continue

        images.append(
            {
                "type": "image",
                "file_path": output_path,
                "path": output_path,
                "image_url": image_url,
                "source_url": candidate.get(
                    "source_url",
                    "",
                ),
                "thumbnail_url": candidate.get(
                    "thumbnail_url",
                    "",
                ),
                "title": title,
                "source": candidate.get(
                    "source",
                    "",
                ),
                "width": width,
                "height": height,
                "sentence_number": (
                    sentence_number
                ),
                "sentence_text": (
                    sentence_text
                ),
                "search_query": query,
                "relevance_score": score,
                "event": get_sentence_event(
                    sentence
                ),
                "entities": get_sentence_entities(
                    sentence
                ),
                "actions": get_sentence_actions(
                    sentence
                ),
            }
        )

    print()
    print(
        f"Sentence {sentence_number} "
        f"images: {len(images)}/{image_count}"
    )

    return images


# ============================================================
# SENTENCE MEDIA
# ============================================================

def process_sentence_media(
    sentence,
    project_dir,
    scene_number,
    image_count=DEFAULT_IMAGE_COUNT,
    video_count=DEFAULT_VIDEO_COUNT,
):
    sentence_number = get_sentence_number(
        sentence,
        1,
    )

    sentence_dir = os.path.join(
        project_dir,
        f"scene_{scene_number}",
        f"sentence_{sentence_number}",
    )

    os.makedirs(
        sentence_dir,
        exist_ok=True,
    )

    sentence_text = get_sentence_text(
        sentence
    )

    print()
    print("=" * 70)
    print(
        f"ASSET MANAGER — SCENE {scene_number} "
        f"— SENTENCE {sentence_number}"
    )
    print("=" * 70)

    print(
        f"Sentence: {sentence_text}"
    )

    print(
        f"Event: "
        f"{get_sentence_event(sentence)}"
    )

    print(
        f"Entities: "
        f"{get_sentence_entities(sentence)}"
    )

    print(
        f"Actions: "
        f"{get_sentence_actions(sentence)}"
    )

    print(
        f"Visual types: "
        f"{get_sentence_visual_types(sentence)}"
    )

    videos = process_sentence_videos(
        sentence,
        sentence_dir,
        video_count,
    )

    images = process_sentence_images(
        sentence,
        sentence_dir,
        image_count,
    )

    visuals = videos + images

    return {
        "sentence_number": sentence_number,
        "sentence_text": sentence_text,
        "images": images,
        "videos": videos,
        "visuals": visuals,
        "sentence": sentence,
    }


# ============================================================
# SCENE MEDIA
# ============================================================

# ============================================================
# FLASH SCENE MEDIA ENGINE
# ============================================================

def process_scene_media(
    scene,
    project_dir,
    image_count=3,
    video_count=1,
):
    """
    FLASH asset engine.

    IMPORTANT:
    This intentionally does NOT process every sentence independently.

    One production scene contains many sentences. Instead of performing
    expensive video research for every sentence, FLASH creates a small
    reusable asset pool for the entire scene.

    Pipeline:

        40 sentences
             |
             v
        scene visual intelligence
             |
             +----> 1 image search
             |
             +----> 1 video search
             |
             v
        small reusable asset pool
             |
             v
        every sentence can reuse those assets

    This eliminates:
        - per-sentence video searches
        - per-sentence transcript extraction
        - hundreds of yt-dlp calls
        - hundreds of FFmpeg calls
    """

    scene_number = scene.get(
        "scene_number",
        1,
    )

    scene_dir = os.path.join(
        project_dir,
        f"scene_{scene_number}",
    )

    os.makedirs(
        scene_dir,
        exist_ok=True,
    )

    print()
    print("=" * 70)
    print(
        f"FLASH ASSET ENGINE — SCENE {scene_number}"
    )
    print("=" * 70)

    # --------------------------------------------------------
    # SENTENCES
    # --------------------------------------------------------

    sentences = get_sentence_list(scene)

    print(
        f"Scene sentences: {len(sentences)}"
    )

    # --------------------------------------------------------
    # BUILD ONE FAST SCENE QUERY
    # --------------------------------------------------------

    entities = []
    events = []
    actions = []
    visual_types = []

    for sentence in sentences:

        for value in get_sentence_entities(sentence):
            if value and value not in entities:
                entities.append(value)

        event = get_sentence_event(sentence)

        if event and event not in events:
            events.append(event)

        for value in get_sentence_actions(sentence):
            if value and value not in actions:
                actions.append(value)

        for value in get_sentence_visual_types(sentence):
            if value and value not in visual_types:
                visual_types.append(value)

        # ========================================================
    # FLASH QUERY BUILDER
    # ========================================================
    #
    # IMPORTANT:
    # Do NOT build the query from all 40 sentences.
    #
    # Later sentences can contain events such as:
    # bankruptcy, collapse, lawsuit, death, etc.
    #
    # Those can completely distort the visual identity of
    # the scene.
    #
    # FLASH therefore prioritizes:
    #
    #   1. Scene title/topic
    #   2. First few sentences
    #   3. Early entities
    #   4. Only then major scene events
    #
    # ========================================================

    query_parts = []

    # --------------------------------------------------------
    # 1. SCENE TITLE / TOPIC
    # --------------------------------------------------------

    scene_title = str(
        scene.get(
            "title",
            "",
        )
    ).strip()

    scene_heading = ""

    if scene_title:
        scene_heading = scene_title

    else:
        scene_text_for_query = str(
            scene.get(
                "text",
                scene.get(
                    "narration",
                    "",
                ),
            )
        )

        # Use the first 250 characters only.
        scene_heading = (
            scene_text_for_query[:250]
        )

    # Extract useful topic words from the heading.
    #
    # These are deliberately conservative so we don't turn
    # the entire scene into a giant search query.

    heading_words = re.findall(
        r"[A-Za-z][A-Za-z'-]{2,}",
        scene_heading,
    )

    stop_words = {
        "the",
        "and",
        "that",
        "this",
        "was",
        "were",
        "with",
        "from",
        "into",
        "when",
        "then",
        "they",
        "their",
        "there",
        "about",
        "have",
        "had",
        "has",
        "for",
        "inside",
        "which",
        "what",
        "where",
        "after",
        "before",
        "because",
        "just",
        "only",
        "roughly",
        "company",
        "story",
        "scene",
    }

    for word in heading_words:

        lower_word = word.lower()

        if lower_word in stop_words:
            continue

        if lower_word not in [
            item.lower()
            for item in query_parts
        ]:
            query_parts.append(
                word
            )

        if len(query_parts) >= 6:
            break

    # --------------------------------------------------------
    # 2. EARLY SENTENCE ENTITIES
    # --------------------------------------------------------
    #
    # Only inspect the first 8 sentences.
    #
    # This prevents later plot developments from hijacking
    # the visual identity of the scene.

    early_entities = []

    for sentence in sentences[:8]:

        for value in get_sentence_entities(
            sentence
        ):

            value = str(
                value
            ).strip()

            if not value:
                continue

            lower_value = (
                value.lower()
            )

            # Reject obvious garbage entity extraction.
            if lower_value in {
                "because",
                "because kodak",
                "the company",
                "the camera",
                "the device",
                "the photograph",
                "this detail",
                "this story",
            }:
                continue

            if lower_value in {
                item.lower()
                for item in early_entities
            }:
                continue

            early_entities.append(
                value
            )

    for value in early_entities:

        if value.lower() not in {
            item.lower()
            for item in query_parts
        }:
            query_parts.append(
                value
            )

        if len(query_parts) >= 8:
            break

    # --------------------------------------------------------
    # 3. EARLY EVENTS ONLY
    # --------------------------------------------------------

    early_events = []

    for sentence in sentences[:8]:

        event = get_sentence_event(
            sentence
        )

        if not event:
            continue

        event = str(
            event
        ).strip()

        if not event:
            continue

        lower_event = event.lower()

        # These are too generic to define the visual subject
        # when they appear late in a scene.

        if lower_event in {
            "collapse",
            "bankruptcy",
            "failure",
            "decline",
            "controversy",
            "lawsuit",
            "investigation",
        }:
            continue

        if lower_event not in {
            item.lower()
            for item in early_events
        }:
            early_events.append(
                event
            )

    for event in early_events[:1]:

        if event.lower() not in {
            item.lower()
            for item in query_parts
        }:
            query_parts.append(
                event
            )

    # --------------------------------------------------------
    # 4. REMOVE BAD QUERY TOKENS
    # --------------------------------------------------------

    bad_query_fragments = {
        "because",
        "because kodak",
        "documentary scene",
        "scene",
        "unknown",
        "none",
        "null",
    }

    cleaned_parts = []

    for value in query_parts:

        value = str(
            value
        ).strip()

        if not value:
            continue

        lower_value = value.lower()

        if lower_value in bad_query_fragments:
            continue

        if lower_value not in {
            item.lower()
            for item in cleaned_parts
        }:
            cleaned_parts.append(
                value
            )

    query_parts = cleaned_parts[:8]

    # --------------------------------------------------------
    # 5. FINAL QUERY
    # --------------------------------------------------------

    scene_query = " ".join(
        query_parts
    ).strip()

    if not scene_query:

        scene_query = (
            f"documentary "
            f"history scene "
            f"{scene_number}"
        )

    print()
    print(
        f"FLASH QUERY: {scene_query}"
    )

    print(
        f"Early entities: "
        f"{early_entities[:6]}"
    )

    print(
        f"Early events: "
        f"{early_events[:4]}"
    )

    # --------------------------------------------------------
    # IMAGE SEARCH
    # --------------------------------------------------------
    #
    # Ask for more candidates than we actually need.
    #
    # If some images are broken, invalid, or inaccessible,
    # FLASH can continue through the candidate pool.

    print()
    print(
        "FLASH IMAGE SEARCH"
    )

    images = []

    # Build a large visual pool for the entire scene.
    # A scene can contain dozens of sentences, so limiting
    # the scene to 3 images causes extreme visual repetition.
    sentence_visual_target = max(
        1,
        min(
            len(sentences),
            40,
        ),
    )

    target_image_count = max(
        image_count,
        sentence_visual_target,
    )

    image_search_limit = max(
        target_image_count * 5,
        30,
    )

    try:

        image_results = search_images(
            scene_query,
            limit=image_search_limit,
        )

    except Exception as error:

        print(
            f"Image search failed: {error}"
        )

        image_results = []

    print(
        f"Image candidates: "
        f"{len(image_results)}"
    )

    for index, result in enumerate(
        image_results,
        start=1,
    ):
        if len(images) >= target_image_count:
            break

        if not isinstance(result, dict):
            continue

        image_url = (
            result.get("image_url")
            or result.get("url")
        )

        if not image_url:
            continue

        output_path = os.path.join(
            scene_dir,
            f"flash_image_{len(images) + 1}.jpg",
        )

        print(
            f"FLASH IMAGE "
            f"{len(images) + 1}/{target_image_count}"
        )

        try:

            success = download_valid_image(
                image_url,
                output_path,
            )

            if not success:
                print(
                    "Image validation failed."
                )
                continue

            image_item = {
                "file_path": output_path,
                "image_url": image_url,
                "source_url": result.get(
                    "source_url",
                    result.get(
                        "url",
                        "",
                    ),
                ),
                "title": result.get(
                    "title",
                    "",
                ),
                "source": result.get(
                    "source",
                    "",
                ),
                "selection_score": result.get(
                    "_asset_score",
                    0,
                ),
                "asset_scope": "scene",
                "scene_number": scene_number,
            }

            images.append(
                image_item
            )

            print(
                f"FLASH IMAGE READY: "
                f"{output_path}"
            )

        except Exception as error:

            print(
                f"FLASH IMAGE FAILED: "
                f"{error}"
            )

    # --------------------------------------------------------
    # VIDEO SEARCH
    # --------------------------------------------------------

    videos = []

    if video_count > 0:

        print()
        print(
            "FLASH VIDEO SEARCH"
        )

        try:

            video_results = search_videos(
                scene_query,
                limit=5,
            )

        except Exception as error:

            print(
                f"Video search failed: {error}"
            )

            video_results = []

        # ----------------------------------------------------
        # HARD SOCIAL-MEDIA FILTER
        # ----------------------------------------------------

        rejected_domains = (
            "instagram.com",
            "facebook.com",
            "tiktok.com",
            "twitter.com",
            "x.com",
            "reddit.com",
            "pinterest.com",
        )

        selected_video = None

        for result in video_results:

            if not isinstance(result, dict):
                continue

            url = result_url(
                result
            )

            if not url:
                continue

            lower_url = url.lower()

            if any(
                domain in lower_url
                for domain in rejected_domains
            ):
                print(
                    f"FLASH VIDEO SKIP "
                    f"(social source): {url}"
                )
                continue

            title = result_title(
                result
            )

            lower_title = (
                title.lower()
            )

            bad_title_terms = (
                "reaction",
                "compilation",
                "top 10",
                "top ten",
                "meme",
                "funny",
                "shorts",
                "tiktok",
                "fan edit",
            )

            if any(
                term in lower_title
                for term in bad_title_terms
            ):
                continue

            selected_video = result
            break

        # ----------------------------------------------------
        # DOWNLOAD ONE VIDEO MAX
        # ----------------------------------------------------

        if selected_video:

            video_url = result_url(
                selected_video
            )

            video_path = os.path.join(
                scene_dir,
                "flash_video_1.mp4",
            )

            print()
            print(
                "FLASH VIDEO SELECTED:"
            )

            print(
                result_title(
                    selected_video
                )
            )

            print(
                video_url
            )

            try:

                success = download_video_clip(
                    video_url,
                    video_path,
                    0,
                    8,
                )

                if success and validate_video(
                    video_path
                ):

                    video_item = {
                        "file_path": video_path,
                        "video_url": video_url,
                        "source_url": video_url,
                        "title": result_title(
                            selected_video
                        ),
                        "source": result_source(
                            selected_video
                        ),
                        "selection_score": selected_video.get(
                            "_asset_score",
                            0,
                        ),
                        "asset_scope": "scene",
                        "scene_number": scene_number,
                        "duration_seconds": 8,
                    }

                    videos.append(
                        video_item
                    )

                    print(
                        "FLASH VIDEO READY:"
                    )

                    print(
                        video_path
                    )

                else:

                    print(
                        "FLASH VIDEO FAILED "
                        "validation."
                    )

            except Exception as error:

                print(
                    f"FLASH VIDEO FAILED: "
                    f"{error}"
                )

        else:

            print(
                "FLASH: No acceptable video "
                "source found."
            )

    # --------------------------------------------------------
    # COMBINED VISUAL POOL
    # --------------------------------------------------------

    all_visuals = (
        videos +
        images
    )

    print()
    print("=" * 70)
    print(
        f"FLASH SCENE {scene_number} COMPLETE"
    )
    print("=" * 70)

    print(
        f"Images downloaded: "
        f"{len(images)}"
    )

    print(
        f"Videos downloaded: "
        f"{len(videos)}"
    )

    print(
        f"Reusable visuals: "
        f"{len(all_visuals)}"
    )

    # --------------------------------------------------------
    # SENTENCE RESULTS
    # --------------------------------------------------------
    #
    # We preserve the structure expected by the timeline.
    #
    # Every sentence points to the same small scene-level asset
    # pool instead of triggering another expensive media search.

    sentence_results = []

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
            f"{sentence_images[0].get('asset_scope', 'none') if sentence_images else 'none'}"
        )


    # --------------------------------------------------------
    # RETURN SAME CONTRACT AS OLD ENGINE
    # --------------------------------------------------------

    return {
        "scene_number": scene_number,
        "images": images,
        "videos": videos,
        "visuals": all_visuals,
        "sentences": sentence_results,
        "scene": scene,
        "asset_engine": "FLASH",
        "asset_scope": "scene_reuse",
        "sentence_count": len(sentences),
    }



