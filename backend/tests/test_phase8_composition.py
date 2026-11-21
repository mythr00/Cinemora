"""Phase 8 — AM-011 completion: split_screen render path and the
Director's footage_window / split_screen triggers.

Phase 6 shipped split_screen as constructor + validation only
(renderable: False) and footage_window with a render path but no
Director trigger. Phase 8 wires both: the split layer renders a
real half-and-half composite through ffmpeg, and the Director
decides window/split compositions under documented, conservative
rules (pack affinities + recorded material only).
"""

import subprocess

from services import composition, visual_director
from services.genre_packs import load_pack


def _make_clip(path, seconds=2, color="navy"):
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"color=c={color}:s=1920x1080:r=30:d={seconds}",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(path),
        ],
        check=True, capture_output=True,
    )
    return path


# ---------------------------------------------------------------------------
# split_screen renders (real ffmpeg)
# ---------------------------------------------------------------------------

def test_split_screen_layer_renders(tmp_path):
    from services.media_probe import probe_video

    base = _make_clip(tmp_path / "base.mp4", seconds=2, color="navy")
    other = _make_clip(tmp_path / "other.mp4", seconds=3, color="maroon")
    layer = composition.split_screen_layer(
        str(other), clip_start=0.5, clip_end=2.5,
        start=0.0, duration=2.0, side="right",
    )
    plan = {"pattern": "split_screen", "layers": [layer]}
    composition.validate_composition(plan, 2.0)
    assert composition.valid_layers(plan) == [layer]
    out = tmp_path / "split.mp4"
    ok, record = composition.apply_composition(
        base, plan, out, tmp_path / "work",
    )
    assert ok is True
    assert record["applied"][0]["kind"] == "split_screen"
    assert record["failed"] == []
    probe = probe_video(out)
    assert probe is not None
    assert abs(probe["duration_seconds"] - 2.0) < 0.35
    assert base.exists() and other.exists()


def test_split_screen_missing_source_fails_honestly(tmp_path):
    base = _make_clip(tmp_path / "base.mp4", seconds=2)
    layer = composition.split_screen_layer(
        str(tmp_path / "does-not-exist.mp4"),
        clip_start=0.0, clip_end=2.0, start=0.0, duration=2.0,
    )
    plan = {"pattern": "split_screen", "layers": [layer]}
    composition.validate_composition(plan, 2.0)
    out = tmp_path / "split.mp4"
    ok, record = composition.apply_composition(
        base, plan, out, tmp_path / "work",
    )
    assert ok is False
    assert record["failed"][0]["kind"] == "split_screen"
    assert not out.exists()
    assert base.exists()


def test_footage_window_layer_renders(tmp_path):
    from services.media_probe import probe_video

    base = _make_clip(tmp_path / "base.mp4", seconds=2, color="navy")
    other = _make_clip(tmp_path / "other.mp4", seconds=3, color="olive")
    layer = composition.footage_window_layer(
        str(other), clip_start=0.5, clip_end=2.5,
        start=0.0, duration=2.0,
    )
    plan = {"pattern": "footage_window", "layers": [layer]}
    composition.validate_composition(plan, 2.0)
    out = tmp_path / "windowed.mp4"
    ok, record = composition.apply_composition(
        base, plan, out, tmp_path / "work",
    )
    assert ok is True
    assert record["applied"][0]["kind"] == "footage_window"
    probe = probe_video(out)
    assert abs(probe["duration_seconds"] - 2.0) < 0.35


# ---------------------------------------------------------------------------
# Director triggers
# ---------------------------------------------------------------------------

def _plan_v1(rows):
    return {
        "version": 1, "project_id": "proj-phase8",
        "analysis_mode": "llm", "scene_governor": None,
        "sentences": rows,
    }


def _row(number, scene=1):
    return {
        "sentence_number": number, "scene_number": scene,
        "role": "statement", "primary_visual_intent": "exact",
        "secondary_visual_intents": [], "intent_queries": {},
        "graphic_fallback": {"allowed": True, "kind": "stat"},
        "preferred_media_type": "video",
        "specificity_requirement": "exact",
        "visual_misrepresentation_risk": "low",
        "intents_source": "llm_plan", "fallback_concepts": None,
        "concepts_source": None, "entry_tier": "direct",
    }


_SECONDARY = {
    "path": "/media/second.mp4", "clip_start": 4.0, "clip_end": 9.5,
    "title": "Second verified angle",
}


def _outcome(number, index=0, text="", title=None, secondary=None):
    return {
        "sentence_number": number, "scene_number": 1,
        "sentence_index": index, "text": text, "scenario": {},
        "covered": True, "starved": False, "tier": "direct",
        "asset_title": title, "asset_file": "/media/asset.mp4",
        "scene_photo_count": 0, "secondary_video": secondary,
    }


def test_director_window_trigger_after_scene_entry_label():
    """General pack: picture_in_picture affinity 1. Beat 1 takes
    the scene-entry lower-third (Phase 6 precedence); beat 2,
    with a secondary verified video in its outcome, takes the
    footage window with the RECORDED window."""
    pack = load_pack("general")
    plan = _plan_v1([_row(1), _row(2)])
    outcomes = {
        1: _outcome(1, title="Harbour at dawn", secondary=_SECONDARY),
        2: _outcome(2, index=1, title="Ferry crossing",
                    secondary=_SECONDARY),
    }
    plan_v2, _manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=pack
    )
    first = plan_v2["sentences"][0]["decision"]["composition"]
    second = plan_v2["sentences"][1]["decision"]["composition"]
    assert first["pattern"] == "lower_third_card"
    assert second["pattern"] == "footage_window"
    assert second["reason"] == "related_verified_footage_window"
    assert second["window_asset"]["path"] == "/media/second.mp4"
    assert second["window_asset"]["clip_start"] == 4.0
    assert second["window_asset"]["clip_end"] == 9.5


def test_director_window_requires_secondary_video():
    pack = load_pack("general")
    plan = _plan_v1([_row(1), _row(2)])
    outcomes = {
        1: _outcome(1, title="Harbour at dawn"),
        2: _outcome(2, index=1, title="Ferry crossing"),
    }
    plan_v2, _manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=pack
    )
    assert "composition" not in plan_v2["sentences"][1]["decision"]


def test_director_window_one_per_scene():
    pack = load_pack("general")
    plan = _plan_v1([_row(1), _row(2), _row(3)])
    outcomes = {
        1: _outcome(1, title="One", secondary=_SECONDARY),
        2: _outcome(2, index=1, title="Two", secondary=_SECONDARY),
        3: _outcome(3, index=2, title="Three", secondary=_SECONDARY),
    }
    plan_v2, _manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=pack
    )
    patterns = [
        (row["decision"].get("composition") or {}).get("pattern")
        for row in plan_v2["sentences"]
    ]
    assert patterns == ["lower_third_card", "footage_window", None]


def _split_pack():
    return {
        "id": "custom",
        "visual_theme": "editorial",
        "visual_profile": {
            "template_affinities": {},
            "composition_affinities": {
                "full_frame": 2, "split_screen": 3,
                "lower_third_card": 1, "picture_in_picture": 1,
            },
        },
    }


def test_director_split_trigger_needs_comparison_and_affinity():
    plan = _plan_v1([_row(1)])
    outcomes = {
        1: _outcome(
            1, text="The east side held 1,204 people; the west 3,860.",
            title="Crowds", secondary=_SECONDARY,
        ),
    }
    plan_v2, _m = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=_split_pack()
    )
    comp = plan_v2["sentences"][0]["decision"]["composition"]
    assert comp["pattern"] == "split_screen"
    assert comp["reason"] == "comparison_split_screen"
    assert comp["split_asset"]["clip_end"] == 9.5


def test_director_split_needs_comparison_signal():
    plan = _plan_v1([_row(1)])
    outcomes = {
        1: _outcome(1, text="The crowd gathered quietly.",
                    title="Crowds", secondary=_SECONDARY),
    }
    plan_v2, _m = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=_split_pack()
    )
    comp = plan_v2["sentences"][0]["decision"].get("composition")
    assert comp is None or comp["pattern"] != "split_screen"


def test_director_split_never_fires_on_shipped_packs():
    """No shipped pack lists split_screen in its affinities, so
    the pattern stays dormant in production until a pack is
    deliberately edited — a data decision, not a code default."""
    for pack_id in ("general", "history", "true_crime"):
        pack = load_pack(pack_id)
        plan = _plan_v1([_row(1)])
        outcomes = {
            1: _outcome(
                1, text="One side counted 40, the other 90.",
                title="Counts", secondary=_SECONDARY,
            ),
        }
        plan_v2, _m = visual_director.reconcile_visual_plan(
            plan, outcomes, genre_pack=pack
        )
        comp = plan_v2["sentences"][0]["decision"].get("composition")
        assert comp is None or comp["pattern"] != "split_screen"


def test_outcomes_adapter_finds_secondary_video():
    processed = [{
        "scene": {"scene_number": 1, "sentences": [
            {"sentence_number": 1, "text": "First."},
            {"sentence_number": 2, "text": "Second."},
        ]},
        "asset_stats": {"missing_visuals": []},
        "videos": [
            {
                "type": "video", "file_path": "/media/a.mp4",
                "title": "A", "sentence_index": 0,
                "clip_start": 0.0, "clip_end": 5.0,
                "verification": {"accepted": True},
            },
            {
                "type": "video", "file_path": "/media/b.mp4",
                "title": "B", "sentence_index": 1,
                "clip_start": 10.0, "clip_end": 16.0,
                "verification": {"accepted": True},
            },
            {
                "type": "video", "file_path": "/media/c.mp4",
                "title": "C — no recorded window",
                "sentence_index": 1,
                "verification": {"accepted": True},
            },
        ],
        "images": [],
    }]
    outcomes = visual_director.outcomes_from_processed_scenes(processed)
    assert outcomes[1]["secondary_video"]["path"] == "/media/b.mp4"
    assert outcomes[1]["secondary_video"]["clip_start"] == 10.0
    assert outcomes[2]["secondary_video"]["path"] == "/media/a.mp4"


# ---------------------------------------------------------------------------
# Decision -> timeline stamping for the new patterns
# ---------------------------------------------------------------------------

def _timeline_with_items(durations):
    return {
        "scenes": [{
            "scene_number": 1,
            "items": [
                {"sentence_index": i, "duration": d,
                 "file_path": f"/media/item{i}.mp4"}
                for i, d in enumerate(durations)
            ],
        }],
    }


def test_stamp_window_and_split_decisions():
    plan_v2 = {
        "version": 2, "theme": None,
        "sentences": [
            {
                "sentence_number": 1, "scene_number": 1,
                "sentence_index": 0,
                "decision": {"treatment": "footage", "composition": {
                    "pattern": "footage_window",
                    "reason": "related_verified_footage_window",
                    "window_asset": {
                        "path": "/media/second.mp4",
                        "clip_start": 4.0, "clip_end": 9.5,
                    },
                }},
            },
            {
                "sentence_number": 2, "scene_number": 1,
                "sentence_index": 1,
                "decision": {"treatment": "footage", "composition": {
                    "pattern": "split_screen",
                    "reason": "comparison_split_screen",
                    "split_asset": {
                        "path": "/media/other.mp4",
                        "clip_start": 1.0, "clip_end": 6.0,
                        "side": "left",
                    },
                }},
            },
        ],
    }
    timeline = _timeline_with_items([4.0, 5.0])
    stamped = composition.apply_decisions_to_timeline(timeline, plan_v2)
    assert stamped == 2
    first, second = timeline["scenes"][0]["items"]
    wlayer = first["composition"]["layers"][0]
    assert wlayer["kind"] == "footage_window"
    assert wlayer["source"]["clip_start"] == 4.0
    assert wlayer["validation"]["passed"] is True
    slayer = second["composition"]["layers"][0]
    assert slayer["kind"] == "split_screen"
    assert slayer["source"]["side"] == "left"
    assert slayer["position"]["x"] == 0  # left half
    assert slayer["validation"]["passed"] is True


def test_stamp_window_decision_with_empty_window_stamps_nothing():
    plan_v2 = {
        "version": 2, "theme": None,
        "sentences": [{
            "sentence_number": 1, "scene_number": 1, "sentence_index": 0,
            "decision": {"treatment": "footage", "composition": {
                "pattern": "footage_window",
                "reason": "related_verified_footage_window",
                "window_asset": {
                    "path": "/media/second.mp4",
                    "clip_start": 9.5, "clip_end": 9.5,
                },
            }},
        }],
    }
    timeline = _timeline_with_items([4.0])
    stamped = composition.apply_decisions_to_timeline(timeline, plan_v2)
    assert stamped == 0
    assert "composition" not in timeline["scenes"][0]["items"][0]
