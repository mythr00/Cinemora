"""Founder defect audit 2026-11-23 — regression pins.

The founder inspected the Container mandate film and banned five
defect classes outright: (1) file names / source labels burned
into graphics, (2) full-narration-sentence text cards, (3) the
"CINEMORA / GENERATED GRAPHIC" corner watermark, (4) footage
that loops/restarts inside its timeline slot instead of playing
as one continuous segment, (5) a photo of an identifiable
different named person placed under a sentence about someone
else. The same audit found three more: drawtext filter-spec
text leaking into frames on apostrophes, charts fabricated from
mixed-unit numbers, and placeholder content (NOW/EVENT,
START/CHANGE/RESULT, "VALUE") rendered as if real.

Every test here pins one of those fixes.
"""

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from services import graphics_pipeline, renderer, story_understanding
from services.motion_engine import templates as engine_templates


TANTLINGER_SENTENCE = (
    "Keith Tantlinger, who was working for McLean at the time, "
    "suggested flipping the design: detachable boxes that could "
    "be lifted on and off ships, trucks, and trains alike."
)

CONTAINER_BIBLE = {
    "title": "The Container",
    "entities": [
        {"id": "mcclean", "canonical": "Malcom McLean",
         "type": "person", "aliases": ["McLean"]},
        {"id": "tantlinger", "canonical": "Keith Tantlinger",
         "type": "person", "aliases": ["Tantlinger"]},
        {"id": "newark", "canonical": "Newark, New Jersey",
         "type": "place", "aliases": ["Newark"]},
        {"id": "idealx", "canonical": "Ideal X",
         "type": "object", "aliases": []},
    ],
    "timeline": [],
}


# ----------------------------------------------------------------
# P0-3: no branding watermark on generated graphics
# ----------------------------------------------------------------

def test_template_footer_draws_nothing():
    """The shared template footer is a no-op: the historical
    "CINEMORA / GENERATED GRAPHIC" corner stamp is gone from
    every template that calls it."""
    img = Image.new("RGB", (640, 360), (10, 20, 30))
    before = img.tobytes()
    engine_templates._footer(img, None)
    assert img.tobytes() == before


def test_no_branding_strings_in_render_modules():
    """No render module carries a drawable CINEMORA brand stamp
    (doc comments explaining the removal are fine; string
    literals that reach a draw call are not — the footer test
    above covers the shared path, this guards reintroduction in
    drawtext/draw.text arguments)."""
    services = Path(__file__).resolve().parent.parent / "services"
    offenders = []
    for path in [
        services / "motion_engine" / "templates.py",
        services / "motion_engine" / "templates_v2.py",
        services / "motion_engine" / "templates_v3.py",
        services / "motion_engine" / "templates_v4.py",
        services / "vector_graphics.py",
        services / "graphics_pipeline.py",
    ]:
        for line in path.read_text().splitlines():
            if "CINEMORA /" in line and (
                "drawtext" in line or ".text(" in line
                or "label=" in line
            ):
                offenders.append(f"{path.name}: {line.strip()[:80]}")
    assert offenders == []


# ----------------------------------------------------------------
# P0-2: no full-sentence text cards
# ----------------------------------------------------------------

def test_short_payload_selects_designed_element_not_sentence():
    assert graphics_pipeline.short_payload_for(
        TANTLINGER_SENTENCE) == "Keith Tantlinger"
    assert graphics_pipeline.short_payload_for(
        "Gabby Petito never got a trial, or a verdict.",
        sentence_record={"entities": [
            {"name": "Gabby Petito", "type": "person"}]},
    ) == "Gabby Petito"
    # A sentence with no selectable short element yields "" —
    # the caller then skips the card rather than printing prose.
    assert graphics_pipeline.short_payload_for(
        "The boxes were loaded quickly and the ship sailed."
    ) == ""


def test_closing_card_refuses_a_full_sentence(tmp_path):
    rendered, meta = engine_templates.TEMPLATES["closing_card"](
        {"quote": TANTLINGER_SENTENCE}, "editorial",
        tmp_path / "card.mp4", duration=1.0,
        sentence_index=0, scene_number=1,
    )
    assert rendered is None
    assert meta["reason"] == "payload_not_short"


def test_text_overlay_refuses_a_full_sentence(tmp_path):
    rendered, meta = engine_templates.TEMPLATES["text_overlay"](
        {"text": TANTLINGER_SENTENCE}, "editorial",
        tmp_path / "overlay.mp4", duration=1.0,
        sentence_index=0, scene_number=1,
    )
    assert rendered is None
    assert meta["reason"] == "payload_not_short"


# ----------------------------------------------------------------
# P0-1: no file names / source labels in graphic inputs
# ----------------------------------------------------------------

def test_metadata_title_detection():
    assert graphics_pipeline.is_metadata_title(
        "File:Barrels and cockpit.png")
    assert graphics_pipeline.is_metadata_title(
        "File:Line3174 - Shipping Containers at the terminal")
    assert graphics_pipeline.is_metadata_title("IMG_4021.JPG")
    assert not graphics_pipeline.is_metadata_title(
        "Dockworkers strike enters second week")
    assert not graphics_pipeline.is_metadata_title("")


def _photo(path, colour):
    img = Image.new("RGB", (800, 600), colour)
    img.save(path)
    return str(path)


def test_built_graphics_carry_no_asset_metadata(tmp_path):
    """End-to-end through build_motion_graphics_assets: the
    evidence board and photo parallax built from Wikimedia-titled
    photos must not carry file names, captions or source labels
    in the inputs the templates render from."""
    photo_a = _photo(tmp_path / "a.jpg", (120, 90, 60))
    photo_b = _photo(tmp_path / "b.jpg", (60, 90, 120))
    verified = {"verification": {"accepted": True}}
    data = {
        "case_bible": CONTAINER_BIBLE,
        "scenes": [{
            "scene_number": 1,
            "sentences": [
                {"text": "Malcom McLean watched the Ideal X load "
                         "at Newark, New Jersey.",
                 "sentence_number": 1},
                {"text": "The port of Newark, New Jersey worked "
                         "through the night.",
                 "sentence_number": 2},
            ],
        }],
        "processed_scenes": [{
            "scene": {"scene_number": 1},
            "videos": [],
            "images": [
                {"file_path": photo_a,
                 "title": "File:Barrels and cockpit.png",
                 "source": "wikimedia", **verified},
                {"file_path": photo_b,
                 "title": "File:Line3174 - Shipping Containers.jpg",
                 "source": "wikimedia", **verified},
            ],
        }],
    }
    targets = [
        {"scene_number": 1, "sentence_index": 0, "sentence_number": 1,
         "text": data["scenes"][0]["sentences"][0]["text"],
         "purpose": "coverage_fallback",
         "template_candidates": ["photo_parallax"],
         "family_candidates": []},
        {"scene_number": 1, "sentence_index": 1, "sentence_number": 2,
         "text": data["scenes"][0]["sentences"][1]["text"],
         "purpose": "coverage_fallback",
         "template_candidates": ["evidence_board"],
         "family_candidates": []},
    ]
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path / "out", mode="balanced", targets=targets,
    )
    built = [a for a in assets if a.get("delivered")]
    assert built, "expected the board/parallax targets to build"
    for asset in built:
        inputs = asset.get("engine_inputs") or {}
        assert "caption" not in inputs
        assert "source" not in inputs
        assert "subtitle" not in inputs
        for photo in inputs.get("photos") or []:
            caption = photo.get("caption") or ""
            assert "File:" not in caption
            assert ".png" not in caption and ".jpg" not in caption
        title = inputs.get("title") or ""
        assert "File:" not in title
    # The whole recorded input set must be free of the source
    # label the old parallax lower-third burned in.
    import json as _json
    blob = _json.dumps([a.get("engine_inputs") for a in built])
    assert "SOURCE" not in blob
    assert "File:" not in blob


# ----------------------------------------------------------------
# P0-4: one placement = one continuous segment (no looping)
# ----------------------------------------------------------------

def test_create_video_clip_plays_once_and_holds_last_frame(tmp_path):
    """A 1s source (0.5s red, 0.5s green) in a 3s slot: the
    frame at 2.5s must be the source's LAST frame (green, held),
    not its first frame again — the historical -stream_loop -1
    restarted the source every second."""
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=red:s=320x180:r=10:d=0.5",
         "-f", "lavfi", "-i", "color=c=green:s=320x180:r=10:d=0.5",
         "-filter_complex", "[0][1]concat=n=2:v=1:a=0",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True,
    )
    out = tmp_path / "clip.mp4"
    assert renderer.create_video_clip(str(src), 3.0, str(out))

    def pixel_at(seconds):
        frame = tmp_path / f"f{seconds}.png"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", str(seconds),
             "-i", str(out), "-frames:v", "1", str(frame)],
            check=True,
        )
        return Image.open(frame).convert("RGB").getpixel((160, 90))

    early = pixel_at(0.2)
    assert early[0] > 150 and early[1] < 100  # red: source start
    late = pixel_at(2.5)
    assert late[1] > 100 and late[0] < 150  # green: held last frame


# ----------------------------------------------------------------
# P0-5: wrong-person protection
# ----------------------------------------------------------------

def test_verifier_rejects_unrelated_person_on_place_subject_beat():
    """The Eleanor Roosevelt shape, replayed against the
    Container bible: the beat's subject is the PLACE Newark, the
    candidate matches the place — and names a person who belongs
    to no bible entity. The historical subject_hit exemption let
    it verify at 0.9; it must now be rejected as
    unrelated_named_person."""
    bible = story_understanding.hydrate_bible(CONTAINER_BIBLE)
    candidate = {
        "title": "File:Eleanor Roosevelt at Reverend Hayes "
                 "Housing Project.jpg",
        "snippet": "Eleanor Roosevelt visiting a housing project "
                   "in Newark, New Jersey",
        "media_type": "image",
    }
    plan = {
        "sentence": "On April 26, 1956, the Ideal X sailed from "
                    "Newark, New Jersey carrying fifty eight "
                    "containers.",
        "subject_id": "newark",
    }
    result = story_understanding.verify_candidate(
        candidate, plan, bible)
    assert result["accepted"] is False
    assert result["rejection_reason"] == "unrelated_named_person"


def test_verifier_person_subject_hit_still_exempt():
    """The tightening must not break the legitimate case: a
    candidate naming the beat's own person subject raises no
    unrelated-person contradiction."""
    bible = story_understanding.hydrate_bible(CONTAINER_BIBLE)
    candidate = {
        "title": "Malcom McLean portrait",
        "snippet": "Malcom McLean, founder of Sea-Land Service",
        "media_type": "image",
    }
    plan = {
        "sentence": "Malcom McLean, a North Carolina trucker, "
                    "had a problem money could not ignore.",
        "subject_id": "mcclean",
    }
    result = story_understanding.verify_candidate(
        candidate, plan, bible)
    assert "unrelated_named_person" not in result["failed_checks"]


def test_photo_person_conflict_graphics_guard():
    mclean_sentence = (
        "Malcom McLean, a North Carolina trucker, had a problem "
        "money could not ignore."
    )
    stranger = {"title": "File:Eleanor Roosevelt at Reverend "
                         "Hayes Housing Project.jpg"}
    assert graphics_pipeline.photo_person_conflict(
        stranger, mclean_sentence, CONTAINER_BIBLE
    ) == "Eleanor Roosevelt"
    # Right story, wrong person: a Tantlinger photo under the
    # McLean sentence is a conflict too.
    colleague = {"title": "Keith Tantlinger portrait"}
    assert graphics_pipeline.photo_person_conflict(
        colleague, mclean_sentence, CONTAINER_BIBLE
    ) == "Keith Tantlinger"
    # The sentence's own person is fine, and a photo whose title
    # names no person raises no conflict.
    own = {"title": "Malcom McLean portrait"}
    assert graphics_pipeline.photo_person_conflict(
        own, mclean_sentence, CONTAINER_BIBLE) == ""
    neutral = {"title": "File:Shipping containers at terminal.jpg"}
    assert graphics_pipeline.photo_person_conflict(
        neutral, mclean_sentence, CONTAINER_BIBLE) == ""


# ----------------------------------------------------------------
# Audit extras: drawtext leak, fabricated charts, placeholders
# ----------------------------------------------------------------

def test_ffmpeg_text_filter_uses_textfile_not_inline_text():
    filt = graphics_pipeline._ffmpeg_text_filter(
        "Malcom McLean's boxes", "100", "200")
    assert "textfile=" in filt
    assert "text='" not in filt


def test_timeline_graphic_apostrophe_label_leaves_top_edge_clean(
    tmp_path,
):
    """The film's chronology graphic burned raw drawtext filter
    text across its top edge because an apostrophe corrupted the
    inline text= value. With textfile= the label renders at its
    node and the top rows stay background."""
    out = tmp_path / "tl.mp4"
    graphics_pipeline._render_timeline_graphic(
        str(out),
        [{"when": "1956", "what": "McLean's boxes reduced costs"}],
        duration=2.0,
    )
    frame = tmp_path / "tl.png"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", "1.5",
         "-i", str(out), "-frames:v", "1", str(frame)],
        check=True,
    )
    img = Image.open(frame).convert("L")
    top = img.crop((0, 0, img.width, 24))
    assert max(top.getdata()) < 60  # background only, no leaked text


def test_series_from_text_requires_one_shared_unit():
    crane = (
        "The crane lifted 80 metres of steel, then made 25 moves "
        "before lunch and 30 moves after."
    )
    assert graphics_pipeline._series_from_text(crane) is None
    growth = (
        "Output rose to 10 percent in March, 20 percent in "
        "April, and 35 percent in May."
    )
    points = graphics_pipeline._series_from_text(growth)
    assert points is not None and len(points) == 3


def test_timeline_and_process_graphics_refuse_empty_content():
    with pytest.raises(ValueError):
        graphics_pipeline._render_timeline_graphic(
            "/tmp/never.mp4", [], duration=2.0)
    with pytest.raises(ValueError):
        graphics_pipeline._render_timeline_graphic(
            "/tmp/never.mp4", [{"when": "", "what": ""}],
            duration=2.0)
    with pytest.raises(ValueError):
        graphics_pipeline._render_process_graphic(
            "/tmp/never.mp4", [], duration=2.0)
