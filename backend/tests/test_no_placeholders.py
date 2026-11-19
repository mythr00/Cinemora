"""The generated text-card / placeholder fallback is permanently
removed (founder directive). These tests pin the guarantees:

- neither the media pipeline nor the renderer still has a card /
  placeholder creation mechanism;
- a beat that retrieves no verified asset records an explicit
  missing visual and fabricates NOTHING;
- the renderer covers a failed item by holding over genuine scene
  media (recorded in the audit), and refuses — loudly — when a
  scene has no genuine media at all;
- the asset report surfaces missing visuals and holdovers.
"""

import uuid
from pathlib import Path

import pytest
from PIL import Image

from services import media_pipeline, renderer
from services.asset_report import build_asset_report


def test_card_mechanisms_no_longer_exist():
    assert not hasattr(media_pipeline, "_write_text_card")
    assert not hasattr(renderer, "create_placeholder_clip")


def _empty_beat_scene(monkeypatch):
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
                "shot_type": "visual",
                "specificity": "exact",
                "required_subject": "Berlin Wall",
                "place": "Berlin",
                "era": "1989",
                "queries": ["berlin wall 1989"],
            }
        ],
        "text": sentence["text"],
        "role": "event",
    }
    scenario = {
        "visual": "Berlin Wall crowds 1989",
        "kind": "event",
        "specificity": "exact",
        "queries": ["berlin wall 1989"],
        "subject_id": "berlin_wall",
    }
    monkeypatch.setattr(media_pipeline, "build_story_beats", lambda s, b: [beat])
    monkeypatch.setattr(media_pipeline, "_load_case_bible", lambda s, p: {})
    monkeypatch.setattr(media_pipeline, "_beat_scenarios", lambda b: [scenario])
    monkeypatch.setattr(
        media_pipeline,
        "_uve_scenario_search",
        lambda sc, cache, pid, shot_need=None: ([], [], []),
    )
    scene = {
        "scene_number": 1,
        "project_id": f"nocards-{uuid.uuid4()}",
        "sentences": [sentence],
    }
    return scene


def test_empty_beat_records_missing_visual_and_creates_no_card(
    tmp_path, monkeypatch
):
    scene = _empty_beat_scene(monkeypatch)
    result = media_pipeline._uve_process_scene_media(scene, str(tmp_path))

    # Nothing was fabricated: no card file, no text_card asset.
    assert not list(tmp_path.rglob("card_*.jpg"))
    for sentence in result["sentences"]:
        for asset in (sentence.get("videos") or []) + (sentence.get("images") or []):
            assert asset.get("source") != "text_card"
            assert asset.get("asset_scope") != "fallback_card"

    # The gap is explicit in the scene stats (and so in the report).
    stats = result["asset_stats"]
    assert stats["text_cards"] == 0
    assert len(stats["missing_visuals"]) == 1
    missing = stats["missing_visuals"][0]
    assert missing["sentence_number"] == 1
    assert missing["reason"] == "no_verified_asset"


def _noise_image(path):
    image = Image.effect_noise((320, 240), 28).convert("RGB")
    image.save(path, "PNG")
    return str(path)


def _item(path, start, sentence_index):
    return {
        "type": "image",
        "file_path": path,
        "start": start,
        "end": start + 1.0,
        "duration": 1.0,
        "sentence_index": sentence_index,
        "sentence": f"Sentence {sentence_index + 1}.",
    }


def test_renderer_holds_over_genuine_media_instead_of_placeholder(tmp_path):
    good = _noise_image(tmp_path / "good.png")
    scene = {
        "scene_number": 1,
        "items": [
            _item(good, 0.0, 0),
            _item(str(tmp_path / "does-not-exist.png"), 1.0, 1),
        ],
    }
    clips, _cursor, audit = renderer.render_scene(scene, str(tmp_path))
    assert len(clips) == 2  # both slots rendered, second via holdover
    assert audit["placeholder_count"] == 0
    assert audit["holdover_count"] == 1
    assert audit["holdover_items"][0]["held_from"] == good


def test_renderer_refuses_when_scene_has_no_genuine_media(tmp_path):
    scene = {
        "scene_number": 1,
        "items": [_item(str(tmp_path / "missing.png"), 0.0, 0)],
    }
    with pytest.raises(RuntimeError, match="refusing to substitute a placeholder"):
        renderer.render_scene(scene, str(tmp_path))


def test_report_surfaces_missing_visuals_and_holdovers():
    scene = {
        "scene_number": 1,
        "videos": [],
        "images": [],
        "sentences": [],
        "asset_stats": {
            "candidates_considered": 3,
            "candidates_rejected": 3,
            "downloads_failed": 0,
            "missing_visuals": [
                {
                    "sentence_number": 2,
                    "reason": "no_verified_asset",
                    "specificity": "exact",
                }
            ],
        },
    }
    report = build_asset_report(
        [scene],
        renderer_audit={"placeholder_count": 0, "holdover_count": 2},
    )
    assert report["missing_visual_count"] == 1
    assert report["missing_visuals"][0]["sentence_number"] == 2
    assert report["missing_visuals"][0]["scene"] == 1
    assert report["holdover_clips"] == 2
    assert report["text_cards"] == 0
