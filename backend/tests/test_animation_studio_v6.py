"""Animation Studio Expansion — v6 PIL treatments (mission §9):
document_examination (zoom-to-legible over a genuine document)
and title_sequence (the staged multi-beat opening title).

Pins: registration, the typed refusal discipline (the genuine
document gate is identical to document_presentation's; the
examine contract is required and validated; a bare title is
refused by the sequence so the card can serve), Director
candidacy under recorded contracts only, and real house-format
renders probed off disk.
"""

from PIL import Image, ImageDraw

from services import visual_director
from services.media_probe import probe_video
from services.motion_engine import templates, templates_v6


def _document_image(path):
    """A synthetic document-page fixture: ruled text lines and
    one dense paragraph block in the lower right quadrant (the
    examination target). A test fixture, not production data."""
    img = Image.new("RGB", (1200, 1600), (250, 248, 240))
    d = ImageDraw.Draw(img)
    for i in range(18):
        y = 120 + i * 62
        d.rectangle([90, y, 1110, y + 16], fill=(70, 68, 62))
    for i in range(8):
        y = 1080 + i * 46
        d.rectangle([640, y, 1110, y + 20], fill=(30, 28, 26))
    img.save(path)
    return str(path)


EXAMINE = {"x": 0.5, "y": 0.65, "w": 0.42, "h": 0.28,
           "label": "The final paragraph"}


def _assert_house_format(path, duration):
    probe = probe_video(path)
    assert probe is not None
    assert probe["width"] == 1920
    assert probe["height"] == 1080
    assert abs(float(probe["fps"]) - 30.0) < 0.6
    assert abs(float(probe["duration_seconds"]) - duration) < 0.25


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_v6_templates_registered():
    assert templates.TEMPLATES["document_examination"] is \
        templates_v6.document_examination
    assert templates.TEMPLATES["title_sequence"] is \
        templates_v6.title_sequence


# ---------------------------------------------------------------------------
# document_examination — refusals
# ---------------------------------------------------------------------------

def test_examination_refuses_without_genuine_image(tmp_path):
    rendered, meta = templates_v6.document_examination(
        {"examine": EXAMINE}, None, tmp_path / "x.mp4", duration=1.0)
    assert rendered is None and meta["reason"] == "document_image_missing"
    rendered, meta = templates_v6.document_examination(
        {"image": str(tmp_path / "nope.png"), "examine": EXAMINE},
        None, tmp_path / "x.mp4", duration=1.0)
    assert rendered is None and meta["reason"] == "document_image_missing"


def test_examination_refuses_without_recorded_region(tmp_path):
    image = _document_image(tmp_path / "doc.png")
    rendered, meta = templates_v6.document_examination(
        {"image": image}, None, tmp_path / "x.mp4", duration=1.0)
    assert rendered is None and meta["reason"] == "examine_region_missing"


def test_examination_refuses_invalid_region(tmp_path):
    image = _document_image(tmp_path / "doc.png")
    for bad in ({"x": 0.9, "y": 0.5, "w": 0.4, "h": 0.3},
                {"x": 0.1, "y": 0.1, "w": 0.01, "h": 0.3},
                {"x": "a", "y": 0.1, "w": 0.3, "h": 0.3}):
        rendered, meta = templates_v6.document_examination(
            {"image": image, "examine": bad},
            None, tmp_path / "x.mp4", duration=1.0)
        assert rendered is None and meta["reason"] == "examine_region_invalid"


def test_examination_renders_house_format_and_records_zoom(tmp_path):
    image = _document_image(tmp_path / "doc.png")
    out = tmp_path / "exam.mp4"
    rendered, meta = templates_v6.document_examination(
        {"image": image, "examine": EXAMINE, "date_label": "1912"},
        None, out, duration=2.0)
    assert rendered is not None, meta
    _assert_house_format(out, 2.0)
    # The settled region reads larger than the full-page view —
    # the legibility evidence rides in the recorded inputs.
    assert meta["inputs"]["zoom_factor"] > 1.5


# ---------------------------------------------------------------------------
# title_sequence — refusals + render
# ---------------------------------------------------------------------------

def test_title_sequence_refuses_bare_title(tmp_path):
    rendered, meta = templates_v6.title_sequence(
        {"title": "The Harbour"}, None, tmp_path / "x.mp4", duration=1.0)
    assert rendered is None
    assert meta["reason"] == "title_sequence_needs_stages"
    rendered, meta = templates_v6.title_sequence(
        {}, None, tmp_path / "x.mp4", duration=1.0)
    assert rendered is None and meta["reason"] == "no_title"


def test_title_sequence_renders_house_format(tmp_path):
    out = tmp_path / "title.mp4"
    rendered, meta = templates_v6.title_sequence(
        {"title": "The Harbour", "kicker": "19th century",
         "date_label": "1897"}, None, out, duration=2.0)
    assert rendered is not None, meta
    _assert_house_format(out, 2.0)
    assert meta["inputs"]["stages"] == 3


# ---------------------------------------------------------------------------
# Director candidacy — recorded contracts only
# ---------------------------------------------------------------------------

def _candidates(text, scenario=None, outcome_extra=None):
    outcome = {"text": text, "scenario": scenario or {},
               "scene_photo_count": 0}
    outcome.update(outcome_extra or {})
    candidates, _affinities = visual_director._template_candidates(
        {}, outcome, None)
    return candidates


def test_director_proposes_examination_only_with_contract():
    base = _candidates("The report was filed.",
                       outcome_extra={"document_asset": {"id": 1}})
    assert "document_presentation" in base
    assert "document_examination" not in base
    with_region = _candidates(
        "The report was filed.", scenario={"examine": EXAMINE},
        outcome_extra={"document_asset": {"id": 1}})
    assert "document_examination" in with_region
    assert with_region.index("document_presentation") < \
        with_region.index("document_examination")


def test_director_proposes_title_sequence_on_opening_beat():
    candidates = _candidates(
        "The harbour opened in 1897.", outcome_extra={"sentence_index": 0})
    assert "title_sequence" in candidates
    assert candidates.index("title_sequence") < \
        candidates.index("archival_title")
    later = _candidates(
        "The harbour opened in 1897.", outcome_extra={"sentence_index": 3})
    assert "title_sequence" not in later


# ---------------------------------------------------------------------------
# layered_photo_motion — the NEW 2.5D treatment (not the removed
# photo_parallax: different name, different mechanism)
# ---------------------------------------------------------------------------

def _photo_fixture(path):
    """A photo-like fixture: gradient sky, ground band, and a
    strong central subject (a dark tower) for the separation."""
    img = Image.new("RGB", (1280, 960))
    d = ImageDraw.Draw(img)
    for y in range(960):
        if y < 640:
            shade = 120 + int(60 * y / 640)
            d.line([(0, y), (1280, y)], fill=(shade - 30, shade - 10, shade + 30))
        else:
            d.line([(0, y), (1280, y)], fill=(88, 80, 66))
    d.rectangle([520, 180, 760, 660], fill=(38, 34, 30))
    d.rectangle([520, 180, 760, 230], fill=(196, 162, 74))
    for row in range(6):
        for col in range(3):
            x = 560 + col * 62
            y = 280 + row * 58
            d.rectangle([x, y, x + 30, y + 34], fill=(222, 196, 120))
    img.save(path)
    return str(path)


def test_layered_photo_motion_registered_and_refuses():
    assert templates.TEMPLATES["layered_photo_motion"] is \
        templates_v6.layered_photo_motion
    rendered, meta = templates_v6.layered_photo_motion(
        {}, None, "unused.mp4", duration=1.0)
    assert rendered is None and meta["reason"] == "photo_image_missing"


def test_layered_photo_motion_renders_with_separation(tmp_path):
    photo = _photo_fixture(tmp_path / "photo.png")
    out = tmp_path / "motion.mp4"
    rendered, meta = templates_v6.layered_photo_motion(
        {"photo": photo}, None, out, duration=1.5)
    assert rendered is not None, meta
    _assert_house_format(out, 1.5)
    fraction = meta["inputs"]["foreground_fraction"]
    assert 0.02 < fraction < 0.85


def test_director_proposes_layered_photo_only_on_intent():
    base = _candidates("The tower stood over the harbour.",
                       outcome_extra={"scene_photo_count": 2})
    assert "layered_photo_motion" not in base
    intended = _candidates(
        "The tower stood over the harbour.",
        scenario={"photo_motion": True},
        outcome_extra={"scene_photo_count": 2})
    assert "layered_photo_motion" in intended
