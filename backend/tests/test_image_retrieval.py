"""AUD-13 regression tests: real image retrieval.

Before this fix the engine had NO real-image path: web/image results were
verified and then discarded, and every still in every output was a PIL
text card. These tests prove a verified image candidate is downloaded,
probed and assigned to its sentence — and that licence is honestly
recorded as "unknown", never fabricated as "royalty-free".
"""

from pathlib import Path

from PIL import Image

from services import media_pipeline, search_provider


def _write_png(path, size=(320, 240)):
    # Noise, not a solid colour: validate_image requires >= 1000 bytes,
    # and a solid-colour image compresses below that.
    Image.effect_noise(size, 64).convert("RGB").save(path)


def test_search_image_sources_normalises_and_keeps_licence_unknown(monkeypatch):
    def fake_search_images(query, limit=20):
        return [
            {
                "title": "Berlin Wall falls, 1989",
                "image_url": "https://cdn.example.org/wall.jpg",
                "source_url": "https://news.example.org/story",
                "url": "https://cdn.example.org/wall.jpg",
                "snippet": "Crowds at the Berlin Wall",
                "source": "news.example.org",
            }
        ]

    monkeypatch.setattr(search_provider, "search_images", fake_search_images)
    results = media_pipeline.search_image_sources("berlin wall 1989", limit=5)
    assert len(results) == 1
    item = results[0]
    assert item["image_url"] == "https://cdn.example.org/wall.jpg"
    assert item["source_url"] == "https://news.example.org/story"
    assert item["type"] == "image"
    assert item["license"] == "unknown"
    assert item["license"] != "royalty-free"
    assert item["rights_status"] == "verify_before_publish"


def test_download_image_candidate_produces_probed_asset(tmp_path, monkeypatch):
    def fake_download_image(url, output_path):
        _write_png(output_path, size=(320, 240))

    monkeypatch.setattr(media_pipeline, "download_image", fake_download_image)
    candidate = {
        "title": "Berlin Wall falls, 1989",
        "image_url": "https://cdn.example.org/wall.jpg",
        "url": "https://cdn.example.org/wall.jpg",
        "source_url": "https://news.example.org/story",
        "source": "news.example.org",
        "license": "unknown",
        "matched_query": "berlin wall 1989",
    }
    asset = media_pipeline._uve_download_image_candidate(
        candidate, tmp_path, sentence_number=3, asset_index=1
    )
    assert asset is not None
    assert asset["type"] == "image"
    assert asset["width"] == 320 and asset["height"] == 240
    assert asset["probed"] is True
    assert asset["license"] == "unknown"
    assert asset["source_url"] == "https://news.example.org/story"
    assert asset["retrieved_at"]
    assert Path(asset["file_path"]).exists()


def test_download_image_candidate_rejects_garbage(tmp_path, monkeypatch):
    def fake_download_image(url, output_path):
        Path(output_path).write_bytes(b"not an image at all" * 100)

    monkeypatch.setattr(media_pipeline, "download_image", fake_download_image)
    candidate = {"image_url": "https://cdn.example.org/fake.jpg", "matched_query": "x"}
    asset = media_pipeline._uve_download_image_candidate(
        candidate, tmp_path, sentence_number=1, asset_index=1
    )
    assert asset is None
    assert list(tmp_path.glob("sentence_*")) == []


def _fake_verify(candidates, scenario, bible, shot_need=None):
    return [
        dict(
            c,
            verification={"accepted": True, "score": 0.9},
            universal_score=0.9,
            relevance_score=0.9,
        )
        for c in candidates
    ]


def test_beat_without_video_uses_real_image_not_card(tmp_path, monkeypatch):
    """End-to-end through _uve_process_scene_media: no video candidates, one
    verified image candidate -> the sentence gets the real image and NO
    text card is generated."""
    sentence = {"sentence_number": 1, "text": "The Berlin Wall fell in 1989."}
    beat = {
        "beat_id": 1,
        "subject_id": "berlin_wall",
        "place": "Berlin",
        "searchable": True,
        "sentence_numbers": [1],
        "sentences": [sentence],
        "shot_needs": [
            {
                "shot_index": 1,
                "shot_type": "photo",
                "specificity": "representative",
                "required_subject": "",
                "place": "Berlin",
                "era": "1989",
                "queries": ["berlin wall 1989 photo"],
            }
        ],
        "text": sentence["text"],
        "role": "event",
    }
    scenario = {
        "visual": "Berlin Wall crowds 1989",
        "kind": "event_photo",
        "specificity": "representative",
        "queries": ["berlin wall 1989 photo"],
        "subject_id": "berlin_wall",
    }
    image_candidate = {
        "title": "Berlin Wall falls, 1989",
        "image_url": "https://cdn.example.org/wall.jpg",
        "url": "https://cdn.example.org/wall.jpg",
        "source_url": "https://news.example.org/story",
        "source": "news.example.org",
        "snippet": "Crowds at the Berlin Wall",
        "license": "unknown",
        "matched_query": "berlin wall 1989 photo",
    }

    monkeypatch.setattr(media_pipeline, "build_story_beats", lambda s, b: [beat])
    monkeypatch.setattr(media_pipeline, "_load_case_bible", lambda s, p: {})
    monkeypatch.setattr(media_pipeline, "_beat_scenarios", lambda b: [scenario])
    monkeypatch.setattr(
        media_pipeline,
        "_uve_scenario_search",
        lambda sc, cache, pid, shot_need=None: ([], [], [image_candidate]),
    )
    monkeypatch.setattr(media_pipeline, "_uve_verify_all", _fake_verify)
    monkeypatch.setattr(
        media_pipeline,
        "download_image",
        lambda url, output_path: _write_png(output_path),
    )

    scene = {"scene_number": 1, "project_id": "imgtest", "sentences": [sentence]}
    result = media_pipeline._uve_process_scene_media(scene, str(tmp_path))

    sentences = result["sentences"]
    assert len(sentences) == 1
    images = sentences[0].get("images") or []
    assert len(images) == 1
    assert images[0]["type"] == "image"
    assert images[0].get("asset_scope") != "fallback_card"
    assert Path(images[0]["file_path"]).exists()
    # No generated text card anywhere in the scene directory.
    assert list((tmp_path / "scene_1").glob("card*")) == []
