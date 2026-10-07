from typing import Dict, List

from services.search_provider import search_web


def build_research_queries(
    narration: str,
    visual_description: str,
) -> List[Dict]:
    """
    Build different research queries for one scene.
    """

    return [
        {
            "type": "general",
            "query": narration,
        },
        {
            "type": "historical",
            "query": f"{narration} history",
        },
        {
            "type": "visual",
            "query": visual_description,
        },
        {
            "type": "archival_photos",
            "query": f"{visual_description} archival photographs",
        },
        {
            "type": "archival_footage",
            "query": f"{visual_description} archival footage",
        },
        {
            "type": "primary_sources",
            "query": f"{narration} official records museum archive",
        },
    ]


def research_scene(scene: Dict) -> Dict:
    """
    Research one scene using multiple research angles.
    """

    query_objects = build_research_queries(
        scene["narration"],
        scene["visual_description"],
    )

    sources = []

    for query_object in query_objects:

        query = query_object["query"]

        try:
            results = search_web(query)

        except Exception as error:
            results = [
                {
                    "title": "Search error",
                    "url": "",
                    "snippet": str(error),
                    "source": "error",
                }
            ]

        for result in results:
            result["research_type"] = query_object["type"]
            result["query"] = query

            sources.append(result)

    return {
        "scene_number": scene["scene_number"],
        "narration": scene["narration"],
        "visual_description": scene["visual_description"],
        "queries": query_objects,
        "sources": sources,
    }