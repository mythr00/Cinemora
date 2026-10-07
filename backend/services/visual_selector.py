import re
from urllib.parse import urlparse


# ============================================================
# DOCUMENTARY SOURCE QUALITY
# ============================================================

DOMAIN_SCORES = {
    # Government / official archives
    ".gov": 100,
    "archives.gov": 100,
    "loc.gov": 100,
    "nps.gov": 100,
    "defense.gov": 100,
    "history.navy.mil": 100,

    # Museums / historical institutions
    "nationalww2museum.org": 98,
    "si.edu": 98,
    "smithsonian": 98,
    "museum": 92,

    # Wikimedia / historical collections
    "wikimedia.org": 90,
    "commons.wikimedia.org": 95,
    "upload.wikimedia.org": 95,

    # Reputable documentary / news sources
    "bbc.com": 92,
    "bbc.co.uk": 92,
    "pbs.org": 92,
    "history.com": 88,
    "historyextra.com": 88,
    "nytimes.com": 86,
    "theguardian.com": 84,
    "apnews.com": 84,
    "reuters.com": 84,
    "vox.com": 78,
    "britannica.com": 82,

    # Professional photography archives
    "alamy.com": 78,
    "gettyimages.com": 78,

    # Other historical sources
    "historicflix.com": 70,
    "allthatsinteresting.com": 65,

    # General reference
    "wikipedia.org": 55,

    # YouTube
    "youtube.com": 50,
    "youtu.be": 50,
}


# ============================================================
# BAD / LOW-QUALITY SOURCES
# ============================================================

BAD_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "reddit.com",
    "pinterest.com",
    "x.com",
    "twitter.com",
}


# ============================================================
# BAD / IRRELEVANT CONTENT
# ============================================================

BAD_KEYWORDS = {
    "superman",
    "batman",
    "avengers",
    "marvel",
    "dc comics",
    "comic",
    "comics",
    "anime",
    "cartoon",
    "fan art",
    "fanart",
    "cosplay",
    "gaming",
    "gameplay",
    "movie poster",
    "movie",
    "tv show",
    "television",
    "poster",
    "wallpaper",
    "meme",
    "celebrity",
    "football",
    "soccer",
    "fifa",
    "wwe",
    "fictional",
    "fiction",
}


# ============================================================
# DOCUMENTARY KEYWORDS
# ============================================================

DOCUMENTARY_KEYWORDS = {
    "archive",
    "archival",
    "archived",
    "historical",
    "history",
    "photograph",
    "photography",
    "photo",
    "footage",
    "film",
    "documentary",
    "museum",
    "collection",
    "record",
    "official",
    "government",
    "military",
    "document",
    "library",
    "cold war",
    "soviet",
    "1983",
    "1980",
    "1970",
    "1960",
    "1950",
    "1940",
    "1930",
    "1920",
}


PHOTO_TERMS = [
    "photograph",
    "photography",
    "photo",
    "portrait",
    "archival photograph",
    "historical photograph",
]

ARCHIVAL_PHRASES = [
    "archive",
    "archival footage",
    "bbc archive",
    "historical footage",
    "original footage",
    "news archive",
    "cold war archive",
    "archive footage",
]


# ============================================================
# TEXT HELPERS
# ============================================================

def safe_text(value) -> str:
    if value is None:
        return ""
    return str(value)


def normalize_text(text) -> str:
    text = safe_text(text).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_domain(url) -> str:
    url = safe_text(url)
    if not url:
        return ""

    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def get_domain_score(*urls) -> int:
    domains = []
    for url in urls:
        domain = extract_domain(url)
        if domain:
            domains.append(domain)

    if not domains:
        return 20

    for domain in domains:
        for bad_domain in BAD_DOMAINS:
            if domain == bad_domain or domain.endswith("." + bad_domain):
                return -100

    best_score = 20

    for domain in domains:
        for key, score in DOMAIN_SCORES.items():
            if key.startswith("."):
                if domain.endswith(key):
                    best_score = max(best_score, score)
            elif domain == key or domain.endswith("." + key) or key in domain:
                best_score = max(best_score, score)

    return best_score


def keyword_overlap(query: str, text: str) -> int:
    query_words = set(normalize_text(query).split())
    text_words = set(normalize_text(text).split())

    if not query_words or not text_words:
        return 0

    return len(query_words.intersection(text_words))


def contains_phrase(normalized_text: str, phrase: str) -> bool:
    phrase = normalize_text(phrase)
    if not phrase:
        return False

    pattern = r"\b" + re.escape(phrase) + r"\b"
    return re.search(pattern, normalized_text) is not None


def contains_bad_content(text: str) -> bool:
    normalized = normalize_text(text)
    for keyword in BAD_KEYWORDS:
        if contains_phrase(normalized, keyword):
            return True
    return False


def count_keyword_hits(normalized_text: str, keywords) -> int:
    hits = 0
    for keyword in keywords:
        if contains_phrase(normalized_text, keyword):
            hits += 1
    return hits


# ============================================================
# IMAGE SCORING
# ============================================================

def score_image(image: dict, query: str = "") -> int:
    title = safe_text(image.get("title"))
    source = safe_text(image.get("source"))
    source_url = safe_text(image.get("source_url"))
    image_url = safe_text(image.get("image_url"))

    combined = " ".join([title, source, source_url, image_url])

    domain_score = get_domain_score(source_url, image_url)
    if domain_score < 0:
        return -100

    if contains_bad_content(combined):
        return -100

    score = domain_score

    overlap = keyword_overlap(query, combined)
    score += min(overlap * 6, 30)

    normalized = normalize_text(combined)

    documentary_hits = count_keyword_hits(normalized, DOCUMENTARY_KEYWORDS)
    score += min(documentary_hits * 5, 25)

    for term in PHOTO_TERMS:
        if contains_phrase(normalized, term):
            score += 5

    return score


# ============================================================
# VIDEO SCORING
# ============================================================

def score_video(video: dict, query: str = "") -> int:
    title = safe_text(video.get("title"))
    source = safe_text(video.get("source"))
    video_url = safe_text(video.get("video_url"))
    snippet = safe_text(video.get("snippet"))
    date = safe_text(video.get("date"))
    source_url = safe_text(video.get("source_url"))

    if "/shorts/" in video_url.lower():
        return -100

    combined = " ".join([title, source, video_url, snippet, date, source_url])
    normalized = normalize_text(combined)

    domain_score = get_domain_score(video_url, source_url)
    if domain_score <= -100:
        return -100

    if contains_bad_content(combined):
        return -100

    score = domain_score

    overlap = keyword_overlap(query, combined)
    score += min(overlap * 7, 35)

    documentary_hits = count_keyword_hits(normalized, DOCUMENTARY_KEYWORDS)
    score += min(documentary_hits * 6, 30)

    for phrase in ARCHIVAL_PHRASES:
        if contains_phrase(normalized, phrase):
            score += 12

    domain = extract_domain(video_url)
    if "youtube.com" in domain or "youtu.be" in domain:
        score -= 10

    return score


# ============================================================
# IMAGE SELECTION
# ============================================================

def deduplicate_images(images: list) -> list:
    seen = set()
    unique = []

    for image in images:
        url = safe_text(image.get("image_url"))
        if not url:
            continue

        normalized_url = url.split("?")[0].lower()
        if normalized_url in seen:
            continue

        seen.add(normalized_url)
        unique.append(image)

    return unique


def select_visual_images(
    images: list,
    query: str = "",
    max_images: int = 3,
    minimum_score: int = 35,
) -> list:
    scored = []

    for image in images:
        score = score_image(image, query)
        if score < minimum_score:
            continue

        item = dict(image)
        item["selection_score"] = score
        scored.append(item)

    scored.sort(
        key=lambda x: x.get("selection_score", 0),
        reverse=True,
    )

    scored = deduplicate_images(scored)
    return scored[:max_images]


# ============================================================
# VIDEO SELECTION
# ============================================================

def deduplicate_videos(videos: list) -> list:
    seen = set()
    unique = []

    for video in videos:
        url = safe_text(video.get("video_url"))
        if not url:
            continue

        normalized_url = url.split("?")[0].lower()
        if normalized_url in seen:
            continue

        seen.add(normalized_url)
        unique.append(video)

    return unique


def select_visual_videos(
    videos: list,
    query: str = "",
    max_videos: int = 3,
    minimum_score: int = 45,
) -> list:
    scored = []

    for video in videos:
        score = score_video(video, query)
        if score < minimum_score:
            continue

        item = dict(video)
        item["selection_score"] = score
        scored.append(item)

    scored.sort(
        key=lambda x: x.get("selection_score", 0),
        reverse=True,
    )

    scored = deduplicate_videos(scored)
    return scored[:max_videos]


# ============================================================
# BACKWARD COMPATIBILITY
# ============================================================

def select_visual_sources(
    sources: list,
    query: str = "",
    max_sources: int = 3,
) -> list:
    scored = []

    for source in sources:
        title = safe_text(source.get("title"))
        url = safe_text(source.get("url"))
        snippet = safe_text(source.get("snippet"))

        combined = " ".join([title, url, snippet])

        score = get_domain_score(url)
        if score < 0:
            continue

        if contains_bad_content(combined):
            continue

        score += min(keyword_overlap(query, combined) * 6, 30)

        item = dict(source)
        item["selection_score"] = score
        scored.append(item)

    scored.sort(
        key=lambda x: x.get("selection_score", 0),
        reverse=True,
    )

    return scored[:max_sources]