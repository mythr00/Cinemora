"""AUD-16 / AUD-21 / AUD-23 regression tests: silent substitutions.

- Verifier-rejected candidates can no longer enter the pick pool
  (the "relaxed" pool is gone).
- The low-confidence arbitrary-window YouTube fallback is off.
- One failing motion graphic no longer kills graphics generation.
- A verified sentence visual beats an assigned motion graphic.
- The asset report counts every substitution honestly, and the final
  auditor refuses to call a substituted video 100% genuine.
"""

import subprocess
import uuid

import pytest

from services import graphics_pipeline, media_pipeline
from services.asset_report import build_asset_report
from services.final_video_auditor import validate_final_video
from services.timeline import build_scene_timeline
from services.visual_registry import VisualRegistry


def _candidate(accepted, score, url="https://youtube.com/watch?v=abc123"):
    return {
        "url": url,
        "video_url": url,
        "title": "Berlin Wall documentary footage",
        "snippet": "Berlin Wall 1989",
        "verification": {"accepted": accepted, "score": score},
        "universal_score": score,
        "relevance_score": score,
    }


def test_rejected_candidates_cannot_be_picked():
    registry = VisualRegistry(project_id=f"test-{uuid.uuid4()}")
    rejected = _candidate(False, 0.50)  # verifier said NO, decent score
    picked = media_pipeline._uve_pick(
        [rejected], {"visual": "Berlin Wall"}, registry, set(), shot_need={}
    )
    assert picked == []


def test_accepted_candidates_are_picked():
    registry = VisualRegistry(project_id=f"test-{uuid.uuid4()}")
    accepted = _candidate(True, 0.90)
    picked = media_pipeline._uve_pick(
        [accepted], {"visual": "Berlin Wall"}, registry, set(), shot_need={}
    )
    assert len(picked) == 1
    assert picked[0]["verification_status"] == "verified"


def test_low_confidence_youtube_fallback_is_off():
    assert media_pipeline.YT_LOW_CONFIDENCE_FALLBACK is False


def test_one_failing_graphic_does_not_kill_graphics(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(graphics_pipeline, "_render_stat_graphic", boom)
    data = {
        "case_bible": {},
        "scenes": [
            {
                "scene_number": 1,
                "sentences": [{"text": "It cost $5 million to build.", "sentence_number": 1}],
            }
        ],
    }
    assets = graphics_pipeline.build_motion_graphics_assets(data, tmp_path, mode="balanced")
    assert assets == []


def test_working_graphic_is_registered(tmp_path, monkeypatch):
    def fake_render(path, *args, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)

    monkeypatch.setattr(graphics_pipeline, "_render_stat_graphic", fake_render)
    data = {
        "case_bible": {},
        "scenes": [
            {
                "scene_number": 1,
                "sentences": [{"text": "It cost $5 million to build.", "sentence_number": 1}],
            }
        ],
    }
    # MG-05 (Motion Graphics programme Phase 1): registration now
    # validates rendered output before an asset is recorded. These
    # are logic tests whose fake renderers write non-media bytes,
    # so the validator is stubbed at the same seam; the real
    # validator is covered by dedicated real-file tests in
    # tests/test_motion_graphics_programme.py.
    monkeypatch.setattr(
        graphics_pipeline, "validate_graphic_output",
        lambda path, expected_duration=None, **kwargs: {
            "passed": True, "failure_kind": None, "detail": "",
            "checks": {}, "probe": None,
        },
    )

    assets = graphics_pipeline.build_motion_graphics_assets(data, tmp_path, mode="balanced")
    assert len(assets) == 1
    assert assets[0]["graphic_type"] == "stat"


def test_verified_visual_beats_motion_graphic(tmp_path):
    verified = tmp_path / "verified.mp4"
    verified.write_bytes(b"\x00" * 2048)
    graphic = tmp_path / "graphic.mp4"
    graphic.write_bytes(b"\x00" * 2048)

    verified_asset = {
        "file_path": str(verified), "path": str(verified), "type": "video",
        # MA-37: a verified asset carries its acceptance record.
        "verification": {"accepted": True},
    }
    graphic_asset = {
        "file_path": str(graphic), "path": str(graphic), "type": "video",
        "motion_graphic": True, "sentence_index": 0,
    }
    scene_media = {
        "scene_number": 1,
        "duration_seconds": 5,
        "videos": [verified_asset, graphic_asset],
        "images": [],
        "sentences": [{"sentence_number": 1, "videos": [verified_asset], "images": []}],
    }
    timings = [{"sentence_index": 0, "text": "It cost $5 million.", "start": 0.0, "end": 4.0}]
    result = build_scene_timeline(scene_media, timings)
    item = result["items"][0]
    assert item["file_path"] == str(verified)
    assert item["assignment"] == "sentence"


def _scenes_fixture():
    return [
        {
            "asset_stats": {
                "candidates_considered": 10,
                "candidates_rejected": 4,
                "downloads_failed": 1,
                "text_cards": 1,
            },
            "videos": [
                {
                    "file_path": "/p/v1.mp4", "type": "video",
                    "verification_status": "verified", "universal_score": 0.9,
                    "license": "unknown", "duration_seconds": 8,
                    "source": "youtube", "video_url": "https://youtube.com/watch?v=1",
                },
                {
                    "file_path": "/p/v2.mp4", "type": "video",
                    "universal_score": 0.2,  # no verification_status: unverified
                    "license": "unknown", "duration_seconds": 8,
                    "source": "youtube", "video_url": "https://youtube.com/watch?v=2",
                },
            ],
            "images": [
                {
                    "file_path": "/p/i1.jpg", "type": "image",
                    "verification_status": "verified", "universal_score": 0.8,
                    "license": "unknown", "source": "news.example.org",
                },
                {
                    "file_path": "/p/card.jpg", "type": "image",
                    "asset_scope": "fallback_card", "source": "text_card",
                },
            ],
            "sentences": [],
        }
    ]


def _timeline_fixture():
    return {
        "scenes": [
            {
                "items": [
                    {"file_path": "/p/v1.mp4", "assignment": "sentence", "duration": 12},
                    {"file_path": "/p/v1.mp4", "assignment": "sentence", "duration": 4},
                    {"file_path": "/p/i1.jpg", "assignment": "rotation", "duration": 4},
                ]
            }
        ]
    }


def test_asset_report_counts_substitutions_honestly():
    report = build_asset_report(
        _scenes_fixture(), _timeline_fixture(), renderer_audit={"placeholder_count": 2}
    )
    assert report["total_retrieved_assets"] == 10
    assert report["total_rejected_assets"] == 4
    assert report["failed_downloads"] == 1
    assert report["total_accepted_assets"] == 4
    assert report["verified_moving_footage"] == 1
    assert report["verified_images"] == 1
    assert report["unverified_assets"] == 1
    assert report["unknown_licensing"] == 3  # v1, v2, i1 — card excluded
    assert report["text_cards"] == 1
    assert report["placeholder_clips"] == 2
    assert report["low_confidence_selections"] == 1  # the unverified v2
    # item 1 loops v1 (12s window > 8s clip), item 2 reuses v1: 2 repeats
    assert report["repeated_footage"] == 2
    assert report["sentence_assignment_accuracy"] == pytest.approx(2 / 3, abs=0.01)
    assert report["authentic"] is False
    assert report["authenticity"]["authentic"] is False


def test_asset_report_genuine_when_no_substitutions():
    scenes = [
        {
            "asset_stats": {
                "candidates_considered": 3, "candidates_rejected": 1,
                "downloads_failed": 0, "text_cards": 0,
            },
            "videos": [
                {
                    "file_path": "/p/v1.mp4", "type": "video",
                    "verification_status": "verified", "universal_score": 0.9,
                    "license": "CC-BY", "duration_seconds": 8, "source": "youtube",
                }
            ],
            "images": [],
            "sentences": [],
        }
    ]
    timeline = {"scenes": [{"items": [{"file_path": "/p/v1.mp4", "assignment": "sentence", "duration": 8}]}]}
    report = build_asset_report(scenes, timeline, renderer_audit={"placeholder_count": 0})
    assert report["authentic"] is True
    assert report["format_mix"]["accepted"].get("footage") == 1
    assert report["unknown_licensing"] == 0


def test_asset_report_unknown_placeholders_block_genuine_claim():
    report = build_asset_report(_scenes_fixture(), _timeline_fixture(), renderer_audit=None)
    assert report["placeholder_clips"] is None
    assert report["authentic"] is False


@pytest.fixture(scope="module")
def tiny_video(tmp_path_factory):
    out = tmp_path_factory.mktemp("audit") / "final.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=1920x1080:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(out),
        ],
        check=True,
        timeout=60,
    )
    return out


def test_final_auditor_reports_substitutions(tiny_video):
    report = build_asset_report(
        _scenes_fixture(), _timeline_fixture(), renderer_audit={"placeholder_count": 2}
    )
    result = validate_final_video(str(tiny_video), asset_report=report)
    assert result["authentic"] is False
    assert any("text_cards" in w for w in result["authenticity_warnings"])
    assert any("placeholder_clips" in w for w in result["authenticity_warnings"])


def test_final_auditor_confirms_genuine(tiny_video):
    scenes = [
        {
            "asset_stats": {"candidates_considered": 1, "candidates_rejected": 0,
                            "downloads_failed": 0, "text_cards": 0},
            "videos": [
                {"file_path": "/p/v1.mp4", "type": "video",
                 "verification_status": "verified", "universal_score": 0.9,
                 "license": "CC-BY", "duration_seconds": 8, "source": "youtube"}
            ],
            "images": [],
            "sentences": [],
        }
    ]
    report = build_asset_report(scenes, {"scenes": [{"items": []}]}, renderer_audit={"placeholder_count": 0})
    result = validate_final_video(str(tiny_video), asset_report=report)
    assert result["authentic"] is True


# ---------------------------------------------------------------------------
# F-4: the format-correction reporting contract. A documentary is judged
# by authenticity and composition — never by its footage percentage.
# ---------------------------------------------------------------------------


def test_photo_and_graphic_led_documentary_is_authentic():
    """Zero moving footage, all visuals verified or identified as
    generated: authentic, with the composition reported neutrally."""
    scenes = [
        {
            "asset_stats": {"candidates_considered": 4, "candidates_rejected": 1,
                            "downloads_failed": 0, "text_cards": 0},
            "videos": [],
            "images": [
                {"file_path": "/p/map.jpg", "type": "image",
                 "verification_status": "verified", "universal_score": 0.9,
                 "license": "CC BY-SA 4.0", "source": "wikimedia",
                 "visual_format": "map"},
                {"file_path": "/p/photo.jpg", "type": "image",
                 "verification_status": "verified", "universal_score": 0.8,
                 "license": "unknown", "source": "news.example.org",
                 "visual_format": "photograph"},
                {"file_path": "/p/mg.mp4", "type": "motion_graphic",
                 "motion_graphic": True, "source": "cinemora_motion_graphics",
                 "generated": True, "visual_format": "graphic"},
            ],
            "sentences": [],
        }
    ]
    report = build_asset_report(scenes, {"scenes": [{"items": []}]},
                                renderer_audit={"placeholder_count": 0})
    assert report["verified_moving_footage"] == 0
    assert report["authentic"] is True
    mix = report["format_mix"]["accepted"]
    assert mix.get("map") == 1
    assert mix.get("photograph") == 1
    assert mix.get("graphic") == 1
    # Rights stay separate from authenticity: the photograph's licence
    # is unknown, and the report says so without tainting authenticity.
    assert report["rights"]["unknown_licences"] == 1
    assert report["rights"]["recorded_licences"] == 1


def test_provider_funnel_and_reliance_warning():
    def scene(provider, n_videos):
        return {
            "asset_stats": {
                "candidates_considered": 10, "candidates_rejected": 2,
                "downloads_failed": 1, "text_cards": 0,
                "providers": {provider: {"discovered": 10, "verified": 8,
                                         "rejected": 2, "downloaded": n_videos,
                                         "download_failed": 1}},
                "download_failures": [{"provider": provider, "url": "https://x/v", "reason": "download_failed"}],
            },
            "videos": [
                {"file_path": f"/p/{provider}{i}.mp4", "type": "video",
                 "verification_status": "verified", "universal_score": 0.9,
                 "license": "unknown", "source": provider, "provider": provider}
                for i in range(n_videos)
            ],
            "images": [],
            "sentences": [],
        }

    report = build_asset_report([scene("youtube", 5)], {"scenes": [{"items": []}]},
                                renderer_audit={"placeholder_count": 0})
    funnel = report["providers"]["funnel"]["youtube"]
    assert funnel["discovered"] == 10
    assert funnel["verified"] == 8
    assert funnel["download_failed"] == 1
    assert funnel["accepted_footage"] == 5
    reliance = report["providers"]["single_provider_reliance"]
    assert reliance["warning"] is True
    assert reliance["provider"] == "youtube"
    assert report["download_failures"][0]["reason"] == "download_failed"

    mixed = build_asset_report(
        [scene("youtube", 2), scene("archive.org", 2)],
        {"scenes": [{"items": []}]}, renderer_audit={"placeholder_count": 0},
    )
    assert mixed["providers"]["single_provider_reliance"]["warning"] is False
