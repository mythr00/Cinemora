import re


STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "was", "were", "is", "are",
    "to", "of", "in", "on", "for", "from", "with", "by", "as", "at",
    "it", "its", "their", "they", "them", "this", "that", "these",
    "those", "one", "once", "eventually", "very", "really",
}


ACTION_MAP = {
    "withdraw": "withdrawal",
    "withdrew": "withdrawal",
    "withdrawing": "withdrawal",
    "withdraws": "withdrawal",
    "filed": "filing",
    "filing": "filing",
    "files": "filing",
    "file": "filing",
    "bought": "purchase",
    "buy": "purchase",
    "buys": "purchase",
    "buying": "purchase",
    "sold": "sale",
    "sell": "sale",
    "sells": "sale",
    "selling": "sale",
    "collapsed": "collapse",
    "collapse": "collapse",
    "collapses": "collapse",
    "launched": "launch",
    "launch": "launch",
    "launches": "launch",
    "acquired": "acquisition",
    "acquire": "acquisition",
    "acquires": "acquisition",
    "arrested": "arrest",
    "arrest": "arrest",
    "arrests": "arrest",
    "killed": "death",
    "killing": "death",
    "died": "death",
    "dies": "death",
    "announced": "announcement",
    "announce": "announcement",
    "announces": "announcement",
    "resigned": "resignation",
    "resign": "resignation",
    "bankrupt": "bankruptcy",
    "bankruptcy": "bankruptcy",
    "landslide": "landslide",
    "investigation": "investigation",
    "investigated": "investigation",
    "closed": "closure",
    "closure": "closure",
    "opened": "opening",
    "opening": "opening",
}


VISUAL_TYPE_RULES = {
    "company": [
        "company", "corporation", "exchange", "bank", "firm",
        "business", "startup",
    ],
    "finance": [
        "money", "financial", "finance", "crypto", "cryptocurrency",
        "exchange", "trading", "stock", "market", "investment", "investor",
    ],
    "legal": [
        "bankruptcy", "court", "lawsuit", "legal", "trial", "judge",
        "filing", "charges", "indictment",
    ],
    "historical_event": [
        "war", "battle", "crisis", "attack", "invasion", "collapse",
        "disaster", "incident", "landslide",
    ],
    "person": [
        "ceo", "president", "founder", "person", "man", "woman",
    ],
    "technology": [
        "computer", "software", "website", "internet", "technology",
        "server", "digital", "platform",
    ],
    "military": [
        "military", "army", "navy", "air force", "missile", "weapon",
        "soldier", "nuclear",
    ],
    "infrastructure": [
        "bridge", "highway", "road", "dam", "reservoir", "tunnel",
        "pier", "span",
    ],
    "geography": [
        "mountain", "river", "slope", "province", "canyon", "valley",
        "landslide", "geological",
    ],
}


KNOWN_ENTITIES = [
    "FTX",
    "Sam Bankman-Fried",
    "Bitcoin",
    "Ethereum",
    "Binance",
    "Coinbase",
    "Apple",
    "Microsoft",
    "Google",
    "Amazon",
    "Meta",
    "Tesla",
    "United States",
    "United Kingdom",
    "Soviet Union",
    "Russia",
    "Ukraine",
    "China",
    "Japan",
    "North Korea",
    "South Korea",
    "Cuba",
    "Turkey",
    "Hongqi Bridge",
    "Sichuan",
    "Maerkang",
    "Barkam",
    "Zumuzu River",
    "Shuangjiangkou",
    "Shuangjiangkou Dam",
    "Shuangjiangkou reservoir",
    "Shuangjiangkou hydropower",
    "Aba",
    "Ngawa",
    "National Highway 317",
    "Tibet",
]


def detect_entities(text):
    found = []
    lowered = text.lower()

    for entity in KNOWN_ENTITIES:
        if entity.lower() in lowered:
            if entity not in found:
                found.append(entity)

    proper_name_pattern = re.compile(
        r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}\b"
    )

    for match in proper_name_pattern.findall(text):
        if match not in found:
            found.append(match)

    return found


def detect_direct_entities(text):
    return detect_entities(text)


def detect_dates(text):
    patterns = [
        r"\b(?:19|20)\d{2}\b",
        r"\b(?:January|February|March|April|May|June|July|"
        r"August|September|October|November|December)"
        r"\s+\d{1,2},?\s+(?:19|20)\d{2}\b",
        r"\b\d{1,2}/\d{1,2}/(?:19|20)\d{2}\b",
    ]

    dates = []
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            if match not in dates:
                dates.append(match)
    return dates


def detect_actions(text):
    words = re.findall(r"[A-Za-z]+", text.lower())
    actions = []
    for word in words:
        action = ACTION_MAP.get(word)
        if action and action not in actions:
            actions.append(action)
    return actions


def extract_keywords(text):
    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]*", text)
    keywords = []
    for word in words:
        normalized = word.lower()
        if normalized in STOPWORDS:
            continue
        if len(normalized) < 3:
            continue
        if word not in keywords:
            keywords.append(word)
    return keywords[:15]


def detect_visual_types(text, entities, actions):
    lowered = text.lower()
    visual_types = []

    for visual_type, terms in VISUAL_TYPE_RULES.items():
        for term in terms:
            if term in lowered:
                if visual_type not in visual_types:
                    visual_types.append(visual_type)
                break

    for entity in entities:
        entity_lower = entity.lower()
        if entity_lower in {
            "ftx", "bitcoin", "ethereum", "binance", "coinbase",
        }:
            if "finance" not in visual_types:
                visual_types.append("finance")
            if "company" not in visual_types:
                visual_types.append("company")
        if "bridge" in entity_lower or "highway" in entity_lower:
            if "infrastructure" not in visual_types:
                visual_types.append("infrastructure")

    if "bankruptcy" in actions:
        if "legal" not in visual_types:
            visual_types.append("legal")
        if "event" not in visual_types:
            visual_types.append("event")

    if "filing" in actions and "legal" not in visual_types:
        visual_types.append("legal")

    if "withdrawal" in actions and "finance" not in visual_types:
        visual_types.append("finance")

    if "landslide" in actions or "collapse" in actions:
        if "historical_event" not in visual_types:
            visual_types.append("historical_event")

    return visual_types


def detect_event_type(text, actions, visual_types):
    lowered = text.lower()

    if "bankruptcy" in lowered:
        return "bankruptcy"
    if "withdraw" in lowered:
        return "customer_withdrawals"
    if "landslide" in lowered:
        return "landslide"
    if "collapse" in lowered:
        return "collapse"
    if "investigation" in lowered or "investigat" in lowered:
        return "investigation"
    if "closed" in lowered or "closure" in lowered:
        return "closure"
    if "opened" in lowered or "opening" in lowered:
        return "opening"
    if "court" in lowered:
        return "court"
    if "lawsuit" in lowered:
        return "lawsuit"
    if "arrest" in lowered:
        return "arrest"
    if "war" in lowered:
        return "war"
    if "battle" in lowered:
        return "battle"
    if "attack" in lowered:
        return "attack"
    if actions:
        return actions[0]
    return None


def build_visual_intent(text, entities, actions, visual_types):
    event_type = detect_event_type(text, actions, visual_types)

    intent = {
        "event": event_type,
        "entities": entities,
        "actions": actions,
        "visual_types": visual_types,
        "preferred_visuals": [],
    }

    entity_text = " ".join(entities).lower()

    # Entity-first preferred visuals (documentary subjects)
    for entity in entities[:4]:
        preferred = [f"{entity}"]
        if event_type:
            preferred.append(f"{entity} {event_type}")
        preferred.append(f"{entity} news")
        preferred.append(f"{entity} documentary")
        for item in preferred:
            if item not in intent["preferred_visuals"]:
                intent["preferred_visuals"].append(item)

    # FTX-specific (only when FTX is actually in the sentence/entities)
    if "ftx" in entity_text:
        if event_type == "bankruptcy":
            for item in [
                "FTX bankruptcy filing",
                "FTX bankruptcy court",
                "FTX Chapter 11",
                "FTX collapse news",
            ]:
                if item not in intent["preferred_visuals"]:
                    intent["preferred_visuals"].append(item)
        elif event_type == "customer_withdrawals":
            for item in [
                "FTX customer withdrawals",
                "FTX withdrawal crisis",
                "FTX exchange withdrawal",
            ]:
                if item not in intent["preferred_visuals"]:
                    intent["preferred_visuals"].append(item)

    if event_type == "bankruptcy" and not entities:
        for item in [
            "bankruptcy filing",
            "bankruptcy court",
            "Chapter 11 bankruptcy",
        ]:
            if item not in intent["preferred_visuals"]:
                intent["preferred_visuals"].append(item)

    if event_type == "landslide":
        for item in [
            "landslide Sichuan",
            "mountain landslide",
            "slope failure",
        ]:
            if item not in intent["preferred_visuals"]:
                intent["preferred_visuals"].append(item)

    # Generic company B-roll ONLY when there are no real entities
    if "company" in visual_types and not entities:
        for item in [
            "company headquarters",
            "company office",
            "company building",
        ]:
            if item not in intent["preferred_visuals"]:
                intent["preferred_visuals"].append(item)

    return intent


def clean_query(query):
    return " ".join(str(query).split()).strip()


def generate_search_queries(
    text,
    entities,
    actions,
    visual_types,
    visual_intent,
    keywords,
):
    queries = []
    event_type = visual_intent.get("event")
    preferred_visuals = visual_intent.get("preferred_visuals", [])

    # 1. Entity-first
    for entity in entities[:4]:
        q = clean_query(entity)
        if q and q not in queries:
            queries.append(q)
        if event_type:
            q = clean_query(f"{entity} {event_type}")
            if q not in queries:
                queries.append(q)

    # 2. Preferred visuals
    for query in preferred_visuals:
        query = clean_query(query)
        if query and query not in queries:
            queries.append(query)

    # 3. Entity + action
    for entity in entities[:3]:
        for action in actions[:2]:
            q = clean_query(f"{entity} {action}")
            if q not in queries:
                queries.append(q)

    # 4. News / footage suffixes
    if entities:
        entity = entities[0]
        if event_type:
            for suffix in ["news footage", "documentary footage", "real footage"]:
                q = clean_query(f"{entity} {event_type} {suffix}")
                if q not in queries:
                    queries.append(q)

    # 5. Keyword query only if no strong entities
    keyword_text = " ".join(keywords[:8])
    if keyword_text and not entities:
        q = clean_query(keyword_text)
        if q and q not in queries:
            queries.append(q)

    return queries[:10]


def analyze_sentence(text, previous_entities=None):
    text = text.strip()

    direct_entities = detect_direct_entities(text)
    entities = list(direct_entities)

    if previous_entities:
        lowered = text.lower()
        contextual_reference = any(
            phrase in lowered
            for phrase in [
                "the company", "the exchange", "the bank", "the firm",
                "the platform", "the bridge", "the reservoir", "the dam",
                "the mountain", "the slope", "customers", "they", "their",
                "its", "he", "she",
            ]
        )
        if contextual_reference:
            for entity in previous_entities:
                if entity not in entities:
                    entities.append(entity)

    dates = detect_dates(text)
    actions = detect_actions(text)
    keywords = extract_keywords(text)
    visual_types = detect_visual_types(text, entities, actions)
    visual_intent = build_visual_intent(text, entities, actions, visual_types)
    search_queries = generate_search_queries(
        text=text,
        entities=entities,
        actions=actions,
        visual_types=visual_types,
        visual_intent=visual_intent,
        keywords=keywords,
    )

    return {
        "text": text,
        "entities": entities,
        "direct_entities": direct_entities,
        "dates": dates,
        "visual_types": visual_types,
        "actions": actions,
        "event": visual_intent.get("event"),
        "visual_intent": visual_intent,
        "keywords": keywords,
        "search_queries": search_queries,
    }


def split_into_sentences(script):
    if not script:
        return []

    script = script.replace("\r\n", "\n").replace("\r", "\n")
    lines = []

    for line in script.split("\n"):
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if re.match(r"^#{1,6}\s+", stripped):
            continue
        if re.match(r"^[-*_]{3,}$", stripped):
            continue
        lines.append(stripped)

    cleaned = "\n".join(lines).strip()
    if not cleaned:
        return []

    cleaned = re.sub(r"\n+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return []

    parts = re.split(r"(?<=[.!?])(?:\s+|$)", cleaned)
    sentences = []

    for part in parts:
        part = part.strip()
        if not part:
            continue
        part = re.sub(r"^#{1,6}\s+", "", part).strip()
        if part:
            sentences.append(part)

    return sentences


def analyze_script(script):
    sentences_text = split_into_sentences(script)
    sentences = []
    previous_entities = []

    for index, text in enumerate(sentences_text):
        result = analyze_sentence(text, previous_entities)
        result["sentence_id"] = index + 1
        sentences.append(result)
        previous_entities = result.get("entities", [])

    return {
        "script": script,
        "sentence_count": len(sentences),
        "sentences": sentences,
    }