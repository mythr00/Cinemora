import re
from dataclasses import dataclass, field
from services.script_analyzer import analyze_sentence


FACE_BLOCK = re.compile(
    r"\b(headshot|portrait of|mugshot of)\b",
    flags=re.IGNORECASE,
)


@dataclass
class VisualRequest:
    query: str
    kind: str = "broll"
    entities: list = field(default_factory=list)
    locations: list = field(default_factory=list)
    reject_faces: bool = False


def detect_beat(text):
    analysis = analyze_sentence(text or "")
    if analysis.get("locations"):
        return "location"
    if analysis.get("actions"):
        return "action"
    if analysis.get("entities"):
        return "entity"
    return "general"


def mentions_named_person(text):
    analysis = analyze_sentence(text or "")
    return bool(analysis.get("entities"))


def requests_for_scene(scene_text):
    analysis = analyze_sentence(scene_text or "")
    requests = []
    for query in analysis.get("search_queries") or []:
        requests.append(
            VisualRequest(
                query=query,
                kind="evidence",
                entities=analysis.get("entities") or [],
                locations=analysis.get("locations") or [],
                reject_faces=False,
            )
        )
    return requests


def reject_candidate(candidate, request=None):
    title = " ".join([
        str(candidate.get("title") or ""),
        str(candidate.get("snippet") or ""),
        str(candidate.get("source_url") or ""),
    ])
    if FACE_BLOCK.search(title) and request and request.reject_faces:
        return True
    return False