"""Format intelligence tests (F-1..F-3).

F-1: every scenario carries a format intent (footage / photograph /
document / map / chart / graphic / any) with a confidence. The intent is
inferred conservatively in degraded mode, validated when the LLM
provides it, and defaults to "any" — pure relevance — when no
communicative signal exists.
"""

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from PIL import Image

from services import media_pipeline
from services.story_understanding import infer_format_intent, normalize_plan


def test_infer_chart_from_statistics():
    intent, conf = infer_format_intent("The project cost $5 million and employed 2,000 workers.", {})
    assert intent == "chart"
    assert conf >= 0.8


def test_infer_document_from_court():
    intent, _ = infer_format_intent("The court sentenced him to twenty years.", {})
    assert intent == "document"


def test_infer_map_from_geography():
    intent, _ = infer_format_intent("The river formed the border between the two regions.", {})
    assert intent == "map"


def test_infer_footage_from_crowd_action():
    intent, _ = infer_format_intent("Crowds gathered and celebrated in the square.", {})
    assert intent == "footage"


def test_infer_any_when_no_signal():
    intent, conf = infer_format_intent("The cat sat quietly on the mat.", {})
    assert intent == "any"
    assert conf == 0.0


def test_normalize_plan_threads_intent_into_scenarios():
    bible = {"by_id": {}, "alias_index": {}}
    raw = {
        "meaning": "cost",
        "role": "fact",
        "scenarios": [
            {
                "visual": "construction site workers",
                "kind": "event",
                "specificity": "representative",
                "search_terms": ["construction workers"],
            }
        ],
    }
    plan = normalize_plan(raw, "The bridge cost $5 million to build.", bible, None)
    scenario = plan["scenarios"][0]
    assert scenario["format_intent"] == "chart"
    assert scenario["format_intent_confidence"] >= 0.8


def test_normalize_plan_validates_llm_intent():
    bible = {"by_id": {}, "alias_index": {}}
    raw = {
        "meaning": "geography",
        "role": "fact",
        "scenarios": [
            {
                "visual": "river valley aerial",
                "kind": "place",
                "specificity": "representative",
                "search_terms": ["river valley"],
                "format_intent": "map",
            }
        ],
    }
    plan = normalize_plan(raw, "The valley lies east of the mountains.", bible, None)
    assert plan["scenarios"][0]["format_intent"] == "map"

    raw["scenarios"][0]["format_intent"] = "hologram"  # not a real intent
    plan = normalize_plan(raw, "The valley lies east of the mountains.", bible, None)
    assert plan["scenarios"][0]["format_intent"] in {"map", "any"}


# ---------------------------------------------------------------------------
# F-2: unified selection. Verified videos and images compete in ONE pool;
# format intent is a bounded ranking bonus (max 0.10), never a mandate and
# never stronger than a clear relevance gap.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _real_clip(tmp_path_factory):
    out = tmp_path_factory.mktemp("f2") / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=15",
            "-c:v", "mpeg4", str(out),
        ],
        check=True,
        timeout=60,
    )
    return out


def _write_png(path, size=(320, 240)):
    Image.effect_noise(size, 64).convert("RGB").save(path)


def _score_verify(candidates, scenario, bible, shot_need=None):
    return [
        dict(
            c,
            verification={"accepted": True, "score": c.get("_test_score", 0.5)},
            universal_score=c.get("_test_score", 0.5),
            relevance_score=c.get("_test_score", 0.5),
        )
        for c in candidates
    ]


def _run_beat(tmp_path, monkeypatch, real_clip, *, intent, intent_conf, video_score, image_score):
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
                "specificity": "representative",
                "required_subject": "",
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
        "specificity": "representative",
        "queries": ["berlin wall 1989"],
        "subject_id": "berlin_wall",
        "format_intent": intent,
        "format_intent_confidence": intent_conf,
    }
    video_candidate = {
        "title": "Berlin Wall falls footage",
        "video_url": "https://vimeo.com/12345",
        "url": "https://vimeo.com/12345",
        "source": "vimeo",
        "provider": "vimeo",
        "snippet": "Berlin Wall crowds",
        "license": "unknown",
        "matched_query": "berlin wall 1989",
        "_test_score": video_score,
    }
    image_candidate = {
        "title": "Berlin Wall falls, 1989",
        "image_url": "https://cdn.example.org/wall.jpg",
        "url": "https://cdn.example.org/wall.jpg",
        "source_url": "https://news.example.org/story",
        "source": "news.example.org",
        "provider": "news.example.org",
        "snippet": "Crowds at the Berlin Wall",
        "license": "unknown",
        "matched_query": "berlin wall 1989",
        "_test_score": image_score,
    }

    monkeypatch.setattr(media_pipeline, "build_story_beats", lambda s, b: [beat])
    monkeypatch.setattr(media_pipeline, "_load_case_bible", lambda s, p: {})
    monkeypatch.setattr(media_pipeline, "_beat_scenarios", lambda b: [scenario])
    monkeypatch.setattr(
        media_pipeline,
        "_uve_scenario_search",
        lambda sc, cache, pid, shot_need=None: ([video_candidate], [], [image_candidate]),
    )
    monkeypatch.setattr(media_pipeline, "_uve_verify_all", _score_verify)

    def fake_clip(url, output_path, start_time=0, duration=8):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(real_clip, output_path)
        return True

    monkeypatch.setattr(media_pipeline, "download_video_clip", fake_clip)
    monkeypatch.setattr(
        media_pipeline, "download_image", lambda url, output_path: _write_png(output_path)
    )

    scene = {
        "scene_number": 1,
        "project_id": f"f2-{uuid.uuid4()}",
        "sentences": [sentence],
    }
    result = media_pipeline._uve_process_scene_media(scene, str(tmp_path))
    return result["sentences"][0]


def test_photograph_intent_tips_close_contest_to_image(tmp_path, monkeypatch, _real_clip):
    sentence = _run_beat(
        tmp_path, monkeypatch, _real_clip,
        intent="photograph", intent_conf=0.9, video_score=0.60, image_score=0.55,
    )
    assert len(sentence.get("images") or []) == 1
    assert sentence.get("videos") in (None, [])


def test_footage_intent_tips_close_contest_to_video(tmp_path, monkeypatch, _real_clip):
    sentence = _run_beat(
        tmp_path, monkeypatch, _real_clip,
        intent="footage", intent_conf=0.9, video_score=0.55, image_score=0.60,
    )
    assert len(sentence.get("videos") or []) == 1
    assert sentence.get("images") in (None, [])


def test_clear_relevance_gap_overrides_intent(tmp_path, monkeypatch, _real_clip):
    sentence = _run_beat(
        tmp_path, monkeypatch, _real_clip,
        intent="photograph", intent_conf=0.9, video_score=0.95, image_score=0.40,
    )
    assert len(sentence.get("videos") or []) == 1


def test_no_intent_means_pure_relevance(tmp_path, monkeypatch, _real_clip):
    # Under the old cascade ANY verified video beat ANY image. Now the
    # stronger image wins when no format intent applies.
    sentence = _run_beat(
        tmp_path, monkeypatch, _real_clip,
        intent="any", intent_conf=0.0, video_score=0.50, image_score=0.70,
    )
    assert len(sentence.get("images") or []) == 1
    assert sentence.get("videos") in (None, [])
