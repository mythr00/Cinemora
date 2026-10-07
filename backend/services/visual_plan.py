from services.script_analyzer import analyze_sentence, analyze_script


FACE_BLOCK_LABEL = "none — no face search"


def _clean(value):
    return " ".join(str(value or "").split()).strip()


def _unique(items, limit=8):
    out = []
    for item in items:
        value = _clean(item)
        if value and value not in out:
            out.append(value)
        if len(out) >= limit:
            break
    return out


def _scene_text(scene):
    if isinstance(scene, str):
        return _clean(scene)

    parts = []
    if scene.get("text"):
        parts.append(scene.get("text"))

    for sentence in scene.get("sentences") or []:
        if isinstance(sentence, dict):
            parts.append(sentence.get("text") or sentence.get("sentence") or "")
        else:
            parts.append(str(sentence or ""))

    return _clean(" ".join(parts))


def _queries_from_text(text):
    analysis = analyze_sentence(text or "")
    queries = list(analysis.get("search_queries") or [])
    entities = analysis.get("entities") or []
    locations = analysis.get("locations") or []
    dates = analysis.get("dates") or []
    actions = analysis.get("actions") or []
    event = analysis.get("event") or ""

    extras = []
    if entities:
        extras.append(f"{entities[0]} archival footage")
        extras.append(f"{entities[0]} photograph")
    if entities and locations:
        extras.append(f"{entities[0]} {locations[0]}")
    if locations:
        extras.append(f"{locations[0]} historical")
    if event:
        extras.append(event)
    if dates and entities:
        extras.append(f"{entities[0]} {dates[0]}")
    if actions and entities:
        extras.append(f"{entities[0]} {actions[0]}")

    return _unique(queries + extras, limit=10), analysis


def plan_scene_visuals(scene):
    text = _scene_text(scene)
    script_analysis = analyze_script(text)
    queries, analysis = _queries_from_text(text)

    sentences = script_analysis.get("sentences") or []
    shots = []
    for index, sentence in enumerate(sentences):
        sentence_text = sentence.get("text", "")
        sentence_queries = sentence.get("search_queries") or []
        if not sentence_queries:
            sentence_queries, _ = _queries_from_text(sentence_text)
        shots.append({
            "shot_number": index + 1,
            "sentence_number": sentence.get("sentence_number", index + 1),
            "text": sentence_text,
            "queries": sentence_queries[:4],
            "entities": sentence.get("entities", []),
            "locations": sentence.get("locations", []),
            "dates": sentence.get("dates", []),
        })

    print("=" * 70)
    print("VISUAL PLAN — CURRENT SCRIPT ONLY")
    print("=" * 70)
    print(f"People: {analysis.get('entities') or [FACE_BLOCK_LABEL]}")
    print(f"Places: {analysis.get('locations') or []}")
    print(f"Dates: {analysis.get('dates') or []}")
    print(f"Queries: {queries}")
    print(f"Shots: {len(shots)}")

    return {
        "text": text,
        "people": analysis.get("entities") or [],
        "places": analysis.get("locations") or [],
        "dates": analysis.get("dates") or [],
        "queries": queries,
        "shots": shots,
        "beats": [],
    }


def queries_for_scene(scene):
    plan = plan_scene_visuals(scene)
    queries = list(plan.get("queries") or [])
    for shot in plan.get("shots") or []:
        for query in shot.get("queries") or []:
            if query not in queries:
                queries.append(query)
    return queries[:20]


def safe_queries_for_text(text):
    queries, _ = _queries_from_text(text)
    return queries