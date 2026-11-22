"""Transformation Phase 1.1 — frame-level visual verification.

Verification used to be metadata-only: the verifier's ``visual``
dimension was ``not_evaluated`` under a previous-era standing
order (AM-028). The founder's complete autonomous takeover
mandate (2026-11-21) supersedes that carve-out; transformation
plan §1.1 evaluates the visual dimension on sampled frames of the
downloaded file itself (services/frame_verification.py), with
deterministic, explainable detectors only:

- burned-in social story cards / caption overlays / meme formats
  and transient channel cards -> typed rejection (meme/social
  formats are BANNED by standing founder ruling);
- OCR geography evidence feeding the existing
  ``contradictory_geography`` class, conservatively;
- portrait sources below the landscape fill threshold ->
  rejection, unless the download path's blurred-fill treatment
  (run-9 fix 5) already filled the frame, which is recorded;
- watermarks -> RECORD ONLY (founder ruling 2026-11-22): the
  frame detector stamps ``watermarked_preview`` exactly like the
  metadata path and never rejects.

The regression corpus is built from real artefacts: the accepted
pool of run e95ac155 (the run-9 Petito film, whose source files
persist under backend/media/<project>/) plus frames extracted
from the delivered film itself, plus synthetic PIL frames for
classes with no real sample. Corpus tests skip when the run-9
media directory is absent (a fresh checkout has no media).
"""

import subprocess
import uuid
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from services import frame_verification as fv
from services import media_pipeline as mp
from services import rejection_diagnostics
from services import story_understanding as story
from services.asset_report import build_asset_report
from services.media_probe import probe_image, probe_video

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = (
    BACKEND_DIR / "media" / "31df4571-c8e1-4bba-be20-fa3a48e2c87f"
)
RUN_ID = "e95ac155-3189-49af-8d63-f30e13e23400"
FILM_PATH = (
    BACKEND_DIR.parent.parent
    / "cinemora-audit"
    / "petito-pipeline-test-film-1.mp4"
)
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _bible():
    return story.hydrate_bible({
        "entities": [
            {"id": "gabby", "canonical": "Gabby Petito",
             "aliases": ["Gabby"], "type": "person"},
            {"id": "brian", "canonical": "Brian Laundrie",
             "aliases": ["Laundrie"], "type": "person"},
            {"id": "moab", "canonical": "Moab, Utah",
             "aliases": [], "type": "place"},
            {"id": "florida", "canonical": "Florida",
             "aliases": [], "type": "place"},
            {"id": "wyoming", "canonical": "Wyoming",
             "aliases": [], "type": "place"},
        ]
    })


def _scenario(**over):
    sc = {
        "subject_id": None,
        "specificity": "representative",
        "search_terms": ["police van driving away"],
        "visual": "a police van driving away after a traffic stop",
        "place": "Moab, Utah",
        "era": "2021",
    }
    sc.update(over)
    return sc


def _font(size):
    if not Path(FONT_PATH).exists():
        pytest.skip("DejaVu font unavailable for synthetic frames")
    return ImageFont.truetype(FONT_PATH, size)


# ---------------------------------------------------------------------------
# Run-9 corpus (real artefacts; skip when the media dir is absent)
# ---------------------------------------------------------------------------

def _corpus_files():
    """{sentence_number: [Path, ...]} for the run-9 film's
    non-graphic media, read from the run's own timeline (a
    sentence can carry both an image and a video item)."""
    import json

    timeline_path = PROJECT_DIR / "runs" / RUN_ID / "timeline.json"
    if not timeline_path.exists():
        return {}
    timeline = json.loads(timeline_path.read_text())
    found = {}
    for scene in timeline.get("scenes", []):
        for item in scene.get("items", []):
            path = item.get("file_path") or item.get("path") or ""
            if "/media/" not in path or "motion_graphic" in path:
                continue
            name = Path(path).name
            if "sentence_" not in name:
                continue
            try:
                number = int(name.split("sentence_")[1].split("_")[0])
            except (IndexError, ValueError):
                continue
            if Path(path).exists():
                found.setdefault(number, []).append(Path(path))
    return found


CORPUS_ALL = _corpus_files()
CORPUS = {n: paths[0] for n, paths in CORPUS_ALL.items()}
requires_corpus = pytest.mark.skipif(
    not CORPUS, reason="run-9 media directory not present"
)

# The run-9 accepted pool, classified by frame inspection (the
# main agent's and the founder's frame-by-frame reviews of the
# film). GOOD assets carry no frame-format defect: they must pass
# the frame stage. (s18's collage is a RELEVANCE defect — the
# wrong story's artwork — which is the metadata layer's call, not
# a frame-format fact; s9's Alamy comp is watermarked, which the
# founder ruled is record-only, and its busy photographic backing
# keeps the frame watermark detector silent by design — the
# metadata agency stamp covers that asset with the same field.)
GOOD_SENTENCES = [1, 2, 5, 6, 7, 9, 11, 12, 13, 14, 15, 18, 19, 22]
GOOD_VIDEO_SENTENCES = [5, 13, 14]


def _asset_for(path):
    kind = "video" if path.suffix == ".mp4" else "image"
    asset = {"file_path": str(path), "type": kind}
    if kind == "video":
        probe = probe_video(path)
        asset["duration_seconds"] = (
            probe["duration_seconds"] if probe else 8.0
        )
    else:
        probe = probe_image(path)
        if probe:
            asset["width"], asset["height"] = probe["width"], probe["height"]
    return asset


def _check(sentence):
    return fv.verify_asset_frames(
        _asset_for(CORPUS[sentence]), _scenario(), _bible()
    )


@requires_corpus
def test_corpus_story_card_video_rejected():
    """Sentence 17: a TikTok-style story card (a DIFFERENT case's
    viral search-party story) burned into a portrait column —
    the founder caught it frame-by-frame. Banned format."""
    record = _check(17)
    assert record["outcome"] == "reject"
    assert record["reason"] == fv.SOCIAL_STORY_CARD_REASON


@requires_corpus
def test_corpus_pov_caption_video_rejected():
    """Sentence 20 (video): a TikTok POV meme clip about a
    different case, with burned-in centred captions."""
    videos = [
        p for p in CORPUS_ALL.get(20, []) if p.suffix == ".mp4"
    ]
    if not videos:
        pytest.skip("sentence-20 video not in corpus")
    record = fv.verify_asset_frames(
        _asset_for(videos[0]), _scenario(), _bible()
    )
    assert record["outcome"] == "reject"
    assert record["reason"] == fv.SOCIAL_CAPTION_REASON


@requires_corpus
def test_corpus_legacy_pillarbox_rejected_for_fill():
    """Sentence 10: a portrait phone clip that run 9's OLD
    normalisation padded with plain black bars. Today's download
    path blurred-fills instead (next test); the baked-black file
    is exactly the defect the fill rule exists to catch."""
    record = _check(10)
    assert record["outcome"] == "reject"
    assert record["reason"] == fv.INSUFFICIENT_FILL_REASON
    assert record["detectors"]["frame_fill"]["flank"] == "black"


@requires_corpus
def test_corpus_blurred_fill_treatment_passes_and_is_recorded(tmp_path):
    """The same portrait source, re-rendered through the CURRENT
    download normalisation (blurred_fill_filter_complex), passes
    with the treatment recorded — integrate, don't duplicate."""
    source = CORPUS[10]
    # Reconstruct the raw portrait: the run-9 file's centre column.
    portrait = tmp_path / "portrait.mp4"
    result = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(source),
         "-vf", "crop=618:1080:651:0", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", "-an", str(portrait)],
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip("ffmpeg crop failed in this environment")
    filled = tmp_path / "filled.mp4"
    result = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(portrait),
         "-filter_complex", mp.blurred_fill_filter_complex(),
         "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-an", str(filled)],
        capture_output=True,
    )
    assert result.returncode == 0
    record = fv.verify_asset_frames(
        _asset_for(filled), _scenario(), _bible()
    )
    assert record["outcome"] == "pass"
    fill = record["detectors"]["frame_fill"]
    assert fill["flank"] == "blurred"
    assert fill["treatment"] == "blurred_fill"


@requires_corpus
@pytest.mark.parametrize("sentence", GOOD_SENTENCES)
def test_corpus_good_assets_pass(sentence):
    if sentence not in CORPUS:
        pytest.skip(f"sentence {sentence} not in corpus")
    record = _check(sentence)
    assert record["outcome"] == "pass", (
        f"sentence {sentence} falsely rejected: {record['reason']}"
    )


@requires_corpus
def test_corpus_good_videos_pass():
    for sentence in GOOD_VIDEO_SENTENCES:
        if sentence not in CORPUS:
            continue
        record = _check(sentence)
        assert record["outcome"] == "pass", (
            f"sentence {sentence} falsely rejected: {record['reason']}"
        )


@requires_corpus
def test_false_reject_rate_over_accepted_pool():
    """The §1.1 acceptance measurement: over the run-9 accepted
    pool's frame-good assets, the frame stage's false-reject rate
    must stay at or below 5%."""
    evaluated = 0
    falsely_rejected = 0
    for sentence in GOOD_SENTENCES:
        if sentence not in CORPUS:
            continue
        evaluated += 1
        if _check(sentence)["outcome"] == "reject":
            falsely_rejected += 1
    assert evaluated >= 10, "corpus too small to measure"
    rate = falsely_rejected / evaluated
    assert rate <= 0.05, f"false-reject rate {rate:.2%} over good pool"


@requires_corpus
def test_corpus_documents_are_not_captions():
    """Sentences 19 (autopsy report) and 22 (verdict form) are
    dense justified document text — legitimate documentary
    material the caption rule must not eat (the justified-block
    guard: wide lines sharing one straight margin)."""
    for sentence in (19, 22):
        if sentence not in CORPUS:
            continue
        record = _check(sentence)
        assert record["outcome"] == "pass"
        assert (
            record["detectors"]["social_caption_overlay"]
            ["justified_block_frames"] >= 1
        )


def test_film_bodycam_segment_frames_rejected(tmp_path):
    """The delivered film's Moab bodycam segment (33.6-36.2s) is
    mostly the Marron True Crime channel card fading in over the
    footage — an untrimmed source intro. Sampled frames from the
    film itself must reject with a typed banned-format reason."""
    if not FILM_PATH.exists():
        pytest.skip("run-9 film not present")
    frames = []
    for index, at in enumerate((33.8, 34.9, 36.0)):
        out = tmp_path / f"frame_{index}.png"
        result = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", str(at),
             "-i", str(FILM_PATH), "-frames:v", "1", str(out)],
            capture_output=True,
        )
        if result.returncode != 0:
            pytest.skip("ffmpeg frame extraction failed")
        with Image.open(out) as img:
            frames.append(img.copy())
    record = fv.verify_frames(
        frames, kind="video", scenario=_scenario(), bible=_bible(),
        ocr_text=None,
    )
    assert record["outcome"] == "reject"
    assert record["reason"] in (
        fv.CHANNEL_CARD_REASON, fv.MEME_FORMAT_FRAME_REASON,
    )


# ---------------------------------------------------------------------------
# Synthetic frames (classes with no real sample, and rule semantics)
# ---------------------------------------------------------------------------

def _story_card_image():
    img = Image.new("RGB", (1920, 1080), (24, 24, 28))
    draw = ImageDraw.Draw(img)
    draw.rectangle([620, 60, 1300, 1020], fill=(38, 38, 44))
    font = _font(40)
    lines = [
        "HE JOINED THE SEARCH", "PARTY LOOKING FOR HER", "BUT HE WAS",
        "ACTUALLY THE ONE", "WHO KNEW WHERE", "THE VAN WAS PARKED",
        "THE WHOLE TIME", "POLICE SAY THE", "STORY NEVER ADDED UP",
    ]
    y = 180
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        width = bbox[2] - bbox[0]
        draw.text(((1920 - width) / 2, y), line, fill=(245, 245, 245),
                  font=font)
        y += 88
    return img


def test_synthetic_story_card_image_rejected():
    record = fv.verify_frames(
        [_story_card_image()], kind="image",
        scenario=_scenario(), bible=_bible(), ocr_text=None,
    )
    assert record["outcome"] == "reject"
    assert record["reason"] == fv.SOCIAL_STORY_CARD_REASON


def test_synthetic_meme_image_rejected():
    img = Image.new("RGB", (1280, 720), (200, 190, 170))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 1280, 60], fill=(0, 0, 0))
    draw.rectangle([0, 660, 1280, 720], fill=(0, 0, 0))
    draw.text((340, 668), "WHEN YOU FIND IT", fill=(255, 255, 255),
              font=_font(44))
    record = fv.verify_frames(
        [img], kind="image", scenario=None, bible=None, ocr_text=None,
    )
    assert record["outcome"] == "reject"
    assert record["reason"] == fv.MEME_FORMAT_FRAME_REASON


def _gradient_photo():
    yy, xx = np.mgrid[0:720, 0:1280]
    rng = np.random.default_rng(3)
    base = 120 - yy * 0.05 + np.sin(xx / 200.0) * 10 + rng.normal(
        0, 3, (720, 1280)
    )
    return Image.fromarray(
        np.clip(base, 0, 255).astype("uint8")
    ).convert("RGB")


def test_synthetic_channel_card_transient_rejected_static_recorded():
    photo = _gradient_photo()
    card = photo.copy()
    draw = ImageDraw.Draw(card)
    draw.text((200, 290), "MARRON", fill=(245, 245, 245),
              font=_font(130))
    transient = fv.verify_frames(
        [photo, card, card], kind="video",
        scenario=None, bible=None, ocr_text=None,
    )
    assert transient["outcome"] == "reject"
    assert transient["reason"] == fv.CHANNEL_CARD_REASON
    # The same card in EVERY frame is the asset's own content (a
    # title still), not an untrimmed intro: recorded, not rejected.
    static = fv.verify_frames(
        [card, card, card], kind="video",
        scenario=None, bible=None, ocr_text=None,
    )
    assert static["outcome"] == "pass"
    assert static["detectors"]["channel_card"]["static_card"] is True


def test_synthetic_watermark_recorded_never_rejected():
    yy, xx = np.mgrid[0:720, 0:1280]
    rng = np.random.default_rng(7)
    base = 150 - yy * 0.08 + np.sin(xx / 300.0) * 8 + rng.normal(
        0, 2, (720, 1280)
    )
    photo = Image.fromarray(
        np.clip(base, 0, 255).astype("uint8")
    ).convert("RGB")
    overlay = Image.new("L", (1280, 720), 0)
    draw = ImageDraw.Draw(overlay)
    draw.text((250, 300), "dreamstime", fill=128, font=_font(92))
    img = Image.composite(
        Image.new("RGB", (1280, 720), (235, 235, 235)), photo, overlay
    )
    record = fv.verify_frames(
        [img], kind="image", scenario=None, bible=None, ocr_text=None,
    )
    # Founder ruling 2026-11-22: watermarked stock is ALLOWED —
    # the stamp is a provenance signal, never a rejection.
    assert record["outcome"] == "pass"
    assert record["watermarked_preview"] is True
    assert record["detectors"]["watermark"]["detected"] is True


def test_portrait_image_below_fill_threshold_rejected():
    img = Image.new("RGB", (600, 1200), (110, 120, 130))
    record = fv.verify_frames(
        [img], kind="image", scenario=None, bible=None,
        ocr_text=None, native_size=(600, 1200),
    )
    assert record["outcome"] == "reject"
    assert record["reason"] == fv.INSUFFICIENT_FILL_REASON


def test_blurred_fill_frames_pass_with_treatment_recorded():
    # Textured portrait content (edges for the fill classifier to
    # measure), composited over its own blurred enlargement —
    # exactly what the download normalisation produces.
    rng = np.random.default_rng(11)
    yy, xx = np.mgrid[0:1080, 0:540]
    textured = np.clip(
        90 + yy * 0.05 + np.sin(xx / 40.0) * 25 + rng.normal(
            0, 9, (1080, 540)
        ),
        0, 255,
    ).astype("uint8")
    content = Image.fromarray(textured).convert("RGB")
    background = content.resize((1920, 1080)).filter(
        ImageFilter.GaussianBlur(18)
    )
    frame = background.copy()
    frame.paste(content, (690, 0))
    record = fv.verify_frames(
        [frame, frame, frame], kind="video",
        scenario=None, bible=None, ocr_text=None,
    )
    assert record["outcome"] == "pass"
    assert record["detectors"]["frame_fill"]["treatment"] == (
        "blurred_fill"
    )


# ---------------------------------------------------------------------------
# Geography via the OCR seam (conservative, typed)
# ---------------------------------------------------------------------------

def test_geography_contradiction_from_frame_text():
    plain = Image.new("RGB", (1280, 720), (110, 120, 130))
    record = fv.verify_frames(
        [plain], kind="image", scenario=_scenario(), bible=_bible(),
        ocr_text="BRIGHTON PIER ENGLAND SEAFRONT",
    )
    assert record["outcome"] == "reject"
    assert record["reason"] == story.CONTRADICTORY_GEOGRAPHY
    assert record["detectors"]["geography"]["outcome"] == "fail"


def test_geography_home_country_text_passes():
    plain = Image.new("RGB", (1280, 720), (110, 120, 130))
    record = fv.verify_frames(
        [plain], kind="image", scenario=_scenario(), bible=_bible(),
        ocr_text="MOAB UTAH MAIN STREET",
    )
    assert record["outcome"] == "pass"
    assert record["detectors"]["geography"]["outcome"] == "pass"


def test_geography_not_evaluated_without_ocr_engine():
    plain = Image.new("RGB", (1280, 720), (110, 120, 130))
    record = fv.verify_frames(
        [plain], kind="image", scenario=_scenario(), bible=_bible(),
        ocr_text=None,
    )
    assert record["outcome"] == "pass"
    assert record["detectors"]["geography"]["outcome"] == (
        "not_evaluated"
    )


def test_ocr_provider_seam_on_real_file(tmp_path):
    path = tmp_path / "plain.jpg"
    Image.new("RGB", (1280, 720), (110, 120, 130)).save(path)
    asset = {
        "file_path": str(path), "type": "image",
        "width": 1280, "height": 720,
    }
    record = fv.verify_asset_frames(
        asset, _scenario(), _bible(),
        ocr_text_provider=lambda paths: "BRIGHTON PIER ENGLAND",
    )
    assert record["outcome"] == "reject"
    assert record["reason"] == story.CONTRADICTORY_GEOGRAPHY
    record = fv.verify_asset_frames(
        asset, _scenario(), _bible(),
        ocr_text_provider=lambda paths: None,
    )
    assert record["outcome"] == "pass"
    assert record["detectors"]["geography"]["outcome"] == (
        "not_evaluated"
    )


# ---------------------------------------------------------------------------
# Registry + report vocabulary
# ---------------------------------------------------------------------------

def test_frame_reasons_registered_and_stage_ordered():
    for reason in (
        fv.SOCIAL_STORY_CARD_REASON,
        fv.SOCIAL_CAPTION_REASON,
        fv.MEME_FORMAT_FRAME_REASON,
        fv.CHANNEL_CARD_REASON,
        fv.INSUFFICIENT_FILL_REASON,
        story.CONTRADICTORY_GEOGRAPHY,
        story.UNRELATED_NAMED_PERSON,
    ):
        assert rejection_diagnostics.is_registered_reason(reason), reason
    order = rejection_diagnostics.STAGE_ORDER
    assert "frame_verification" in order
    assert order.index("transcript_verification") < order.index(
        "frame_verification"
    ) < order.index("technical_validation")
    stages = rejection_diagnostics.REJECTION_REASON_REGISTRY[
        fv.SOCIAL_CAPTION_REASON
    ]
    assert stages == ("frame_verification",)


def test_asset_report_visual_frame_block():
    report = build_asset_report([], genre_pack=None)
    block = report["verification_dimensions"]["visual_frame"]
    assert "AM-028" in block["authority"]
    assert block["evaluated_assets"] == 0
    assert block["rejected_events"] == 0
    assert block["watermarked_preview_detected"] == 0


def test_metadata_verdict_visual_stays_not_evaluated():
    """Eligibility stays separate: the METADATA verdict still
    records visual as not_evaluated (truthful — no frames exist
    yet); the frame stage overwrites it only on downloaded
    assets' verdict copies."""
    verdict = story.verify_candidate(
        {
            "title": "Police van driving away in Moab Utah 2021",
            "url": "https://example.com/van.jpg",
            "source": "web",
        },
        _scenario(), _bible(),
    )
    assert verdict["dimension_results"]["visual"] == "not_evaluated"


# ---------------------------------------------------------------------------
# Pick-flow integration: demotion inside _uve_fetch_first
# ---------------------------------------------------------------------------

def _verdict():
    return {
        "accepted": True,
        "score": 0.9,
        "rejection_reason": "",
        "dimension_results": {"visual": "not_evaluated"},
    }


def test_fetch_first_demotes_story_card_and_accepts_clean(tmp_path):
    bad_path = tmp_path / "card.jpg"
    _story_card_image().save(bad_path)
    good_path = tmp_path / "photo.jpg"
    Image.new("RGB", (1280, 720), (110, 120, 130)).save(good_path)

    def _asset(path, url):
        return {
            "url": url,
            "image_url": url,
            "file_path": str(path),
            "path": str(path),
            "type": "image",
            "visual_type": "image",
            "width": 1920 if "card" in url else 1280,
            "height": 1080 if "card" in url else 720,
            "verification": _verdict(),
            "title": path.name,
            "provider": "web",
            "candidate_id": url,
        }

    assets = {
        "https://example.com/card": _asset(
            bad_path, "https://example.com/card"
        ),
        "https://example.com/photo": _asset(
            good_path, "https://example.com/photo"
        ),
    }
    candidates = [
        {
            "url": url,
            "candidate_id": url,
            "provider": "web",
            "verification": _verdict(),
        }
        for url in assets
    ]

    def downloader(candidate, scene_dir, sentence_number, asset_index):
        return assets[candidate["url"]]

    failures = []
    registry = mp.get_registry(f"phase1-{uuid.uuid4()}")
    result = mp._uve_fetch_first(
        candidates,
        downloader,
        str(tmp_path),
        1,
        1,
        registry,
        set(),
        failures=failures,
        transcript_scenario=_scenario(),
        transcript_bible=_bible(),
    )
    assert result is assets["https://example.com/photo"]
    # The story card was demoted with its typed reason + evidence,
    # its file removed, and its verdict copy's visual dimension
    # overwritten with the evaluated outcome.
    assert len(failures) == 1
    failure = failures[0]
    assert failure["stage"] == "frame_verification"
    assert failure["reason"] == fv.SOCIAL_STORY_CARD_REASON
    assert failure["frame_check"]["outcome"] == "reject"
    assert not bad_path.exists()
    bad_candidate = candidates[0]
    assert bad_candidate["verification"]["dimension_results"][
        "visual"
    ] == "fail"
    # The clean asset passed with the dimension evaluated.
    assert result["frame_check"]["outcome"] == "pass"
    good_candidate = candidates[1]
    assert good_candidate["verification"]["dimension_results"][
        "visual"
    ] == "pass"
    assert good_path.exists()
