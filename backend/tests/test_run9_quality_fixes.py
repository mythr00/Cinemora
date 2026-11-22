"""Run-9 quality-fix regressions (2026-11-22).

Production run e95ac155 (Gabby Petito pipeline film) completed the
pipeline but frame inspection found systemic defects. Each test here
pins one defect class to the machinery that now prevents it:

1. Watermarked stock previews (Alamy comp on the Moab beat).
2. Wrong-geography / unrelated-person visuals passing verification.
3. Social-platform and meme-format sources accepted as footage.
4. Chronology graphic truncated by its timeline slot.
5. Aspect-mismatched sources padded with plain black bars.
6. Project category never reaching genre-pack selection.
"""

import subprocess

import pytest

from services import genre_packs
from services import media_pipeline as mp
from services import story_understanding as story


def _petito_bible():
    return story.hydrate_bible({
        "entities": [
            {"id": "gabby", "canonical": "Gabby Petito", "aliases": ["Gabby"], "type": "person"},
            {"id": "brian", "canonical": "Brian Laundrie", "aliases": ["Laundrie"], "type": "person"},
            {"id": "moab", "canonical": "Moab, Utah", "aliases": [], "type": "place"},
            {"id": "florida", "canonical": "Florida", "aliases": [], "type": "place"},
            {"id": "blue_point", "canonical": "Blue Point, New York", "aliases": ["Blue Point"], "type": "place"},
            {"id": "wyoming", "canonical": "Wyoming", "aliases": [], "type": "place"},
        ]
    })


def _scenario(**over):
    sc = {
        "subject_id": None,
        "specificity": "representative",
        "search_terms": ["police van driving away"],
        "visual": "a police van driving away after officers separated the couple",
        "place": "Moab, Utah",
        "era": "2021",
    }
    sc.update(over)
    return sc


# ------------------------------------------------------------- fix 1

def test_alamy_candidate_rejected_as_watermarked_stock():
    """The exact run-9 sentence-9 candidate: Alamy provider, Alamy
    comp CDN host, watermarked preview by construction."""
    verdict = story.verify_candidate(
        {
            "title": "Brighton UK 18th February 2021 - A man (face has been "
                     "pixelated) is detained in a police van",
            "url": "https://l450v.alamy.com/450v/2egbea2/brighton-uk-police.jpg",
            "provider": "Alamy",
        },
        _scenario(), _petito_bible(),
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == story.WATERMARKED_STOCK_REASON


def test_stock_agency_url_rejected_even_with_innocent_provider_label():
    verdict = story.verify_candidate(
        {
            "title": "Police van driving away at night",
            "url": "https://www.shutterstock.com/image-photo/police-van-12345",
            "provider": "serper",
        },
        _scenario(), _petito_bible(),
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == story.WATERMARKED_STOCK_REASON


def test_watermark_word_in_metadata_rejected():
    assert story.is_watermarked_stock(
        {"title": "Police van (watermarked preview)", "url": "https://x.example/a.jpg"}
    )


def test_news_image_not_flagged_as_stock():
    assert not story.is_watermarked_stock(
        {
            "title": "Police van in Moab, Utah",
            "url": "https://cdn.example-news.com/images/van.jpg",
            "provider": "example-news.com",
        }
    )


# ------------------------------------------------------------- fix 2

def test_brighton_uk_is_a_geography_contradiction_for_a_us_case():
    """Even stripped of the Alamy signals, 'Brighton UK' metadata
    positively contradicts a production whose bible is US-only."""
    verdict = story.verify_candidate(
        {
            "title": "Brighton UK 18th February 2021 - a man is detained "
                     "in a police van",
            "url": "https://example-news.com/brighton-detention",
            "provider": "example-news.com",
        },
        _scenario(), _petito_bible(),
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == story.CONTRADICTORY_GEOGRAPHY


def test_us_candidate_not_geography_rejected_for_us_case():
    verdict = story.verify_candidate(
        {
            "title": "Police van driving away on a Utah highway, 2021",
            "snippet": "Officers in Moab, Utah separated the couple; the van drove away",
            "url": "https://example-news.com/moab-van",
            "provider": "example-news.com",
        },
        _scenario(), _petito_bible(),
    )
    assert story.CONTRADICTORY_GEOGRAPHY not in verdict["failed_checks"]


def test_geography_never_fires_for_multi_country_production():
    bible = story.hydrate_bible({
        "entities": [
            {"id": "london", "canonical": "London", "aliases": [], "type": "place"},
            {"id": "nyc", "canonical": "New York", "aliases": [], "type": "place"},
        ]
    })
    reason, _ = story.detect_contradiction(
        {"title": "Crowds in London", "url": "https://x.example/a"},
        {"place": "London", "visual": "crowds", "era": ""},
        bible, story._norm("Crowds in London"), None, False,
    )
    assert reason != story.CONTRADICTORY_GEOGRAPHY


def test_unrelated_named_person_rejected():
    """The Tristyn Bailey shape: the title names a person from a
    different case; no bible person is mentioned at all."""
    verdict = story.verify_candidate(
        {
            "title": "On May 9, 2021, 13-year-old Tristyn Bailey was "
                     "reported missing in Florida",
            "url": "https://www.google.com/goto?url=CAESaAHEIkXX",
            "provider": "google.com",
        },
        _scenario(
            search_terms=["wooded evidence search"],
            visual="searchers searching a wooded area for evidence",
            place="Florida",
        ),
        _petito_bible(),
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == story.UNRELATED_NAMED_PERSON


def test_official_introduced_by_role_word_not_flagged():
    """News naming a case official is reporting about the case, not
    a different case: the role word exempts the name."""
    name = story._unrelated_named_person(
        {"title": "Sheriff Kurt Hoffman gives update on the search"},
        _petito_bible(), story._norm("Sheriff Kurt Hoffman gives update on the search"),
        False,
    )
    assert name == ""


def test_bible_person_in_title_not_flagged():
    name = story._unrelated_named_person(
        {"title": "Brian Laundrie sought in Florida nature reserve"},
        _petito_bible(), story._norm("Brian Laundrie sought in Florida nature reserve"),
        False,
    )
    assert name == ""


# ------------------------------------------------------------- fix 3

def test_facebook_video_rejected_as_social_platform():
    verdict = story.verify_candidate(
        {
            "title": "In September 2021, a man in Turkey found himself "
                     "at the centre of a search",
            "url": "https://www.facebook.com/61573989182885/posts/search",
        },
        _scenario(), _petito_bible(),
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == story.SOCIAL_PLATFORM_REASON


def test_tiktok_and_shorts_urls_rejected():
    assert story.is_social_platform_source(
        {"url": "https://www.tiktok.com/@user/video/123"}
    )
    assert story.is_social_platform_source(
        {"url": "https://www.youtube.com/shorts/abc123"}
    )
    assert story.is_social_platform_source(
        {"url": "https://www.instagram.com/reel/abc123/"}
    )


def test_youtube_watch_url_is_not_a_social_reject():
    assert not story.is_social_platform_source(
        {"url": "https://www.youtube.com/watch?v=-6O2PuWFywA"}
    )


def test_pov_caption_title_rejected_as_meme_format():
    verdict = story.verify_candidate(
        {
            "title": "POV: It's late at night, you see a boy running "
                     "from the woods",
            "url": "https://www.youtube.com/watch?v=abc123",
        },
        _scenario(), _petito_bible(),
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == story.MEME_FORMAT_REASON


def test_news_title_mentioning_pov_mid_title_not_flagged():
    assert not story.has_meme_format_title(
        {"title": "Police release POV bodycam video of the traffic stop"}
    )


# ------------------------------------------------------------- fix 6

_PETITO_SCRIPT = (
    "In the summer of 2021, Gabby Petito and Brian Laundrie traveled "
    "across America in a white van. Police in Moab stopped the van. "
    "Her remains were found in October."
)


def test_project_category_true_crime_resolves_true_crime_pack():
    """Run 9's exact failure: category 'true_crime' on the project,
    no genre in the run options -> the pack must be true_crime, not
    the 'general' default."""
    info = genre_packs.resolve_genre_pack(
        {"project_category": "true_crime"}, _PETITO_SCRIPT
    )
    assert info["pack_id"] == "true_crime"
    assert info["source"] == "project_category"


def test_project_category_beats_script_detection():
    info = genre_packs.resolve_genre_pack(
        {"project_category": "history"}, _PETITO_SCRIPT
    )
    assert info["pack_id"] == "history"
    assert info["source"] == "project_category"


def test_explicit_genre_option_still_beats_project_category():
    info = genre_packs.resolve_genre_pack(
        {"genre": "history", "project_category": "true_crime"},
        _PETITO_SCRIPT,
    )
    assert info["pack_id"] == "history"
    assert info["source"] == "option"


def test_unknown_project_category_falls_through_to_default():
    info = genre_packs.resolve_genre_pack(
        {"project_category": "General documentary"},
        "A quiet film about bread making and village life.",
    )
    assert info["pack_id"] == "general"
    assert info["source"] == "default"


def test_true_crime_pack_broll_band_is_the_founder_band():
    pack = genre_packs.load_pack("true_crime")
    band = genre_packs.broll_share_band(pack)
    assert band["max"] == 0.4
    assert band["basis"] == "founder_band"


# ------------------------------------------------------------- fix 5

def test_blurred_fill_filter_has_no_black_pad():
    graph = mp.blurred_fill_filter_complex()
    assert "pad=" not in graph
    assert "boxblur" in graph
    assert "overlay" in graph


def _normalize_with_pipeline_filter(src, dst):
    """Run the exact filtergraph download_video_clip builds."""
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(src), "-t", "1", "-an",
            "-filter_complex", mp.blurred_fill_filter_complex(),
            "-map", "[v]", "-r", "30", "-c:v", "libx264",
            "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(dst),
        ],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-1500:]


def _frame_pixel(video, xy):
    from PIL import Image
    import io

    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-frames:v", "1",
         "-f", "image2pipe", "-vcodec", "png", "-"],
        capture_output=True, timeout=60,
    )
    assert result.returncode == 0
    image = Image.open(io.BytesIO(result.stdout)).convert("RGB")
    assert image.size == (1920, 1080)
    return image.getpixel(xy)


@pytest.mark.parametrize("size", ["720x1280", "960x720"])
def test_aspect_mismatch_sources_get_blurred_fill_not_black_bars(tmp_path, size):
    """Portrait (720x1280) and 4:3 (960x720) red sources: the frame
    edges must carry the blurred source colour, not black bars,
    while the centre keeps the untouched foreground colour."""
    src = tmp_path / "src.mp4"
    made = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"color=c=red:size={size}:duration=2:rate=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        capture_output=True, text=True, timeout=120,
    )
    assert made.returncode == 0, made.stderr[-1000:]
    dst = tmp_path / "out.mp4"
    _normalize_with_pipeline_filter(src, dst)
    corner = _frame_pixel(dst, (30, 30))
    centre = _frame_pixel(dst, (960, 540))
    # Blurred + darkened red fill: clearly red, clearly not black.
    assert corner[0] > 120 and corner[1] < 110 and corner[2] < 110
    # Foreground intact.
    assert centre[0] > 200 and centre[1] < 80 and centre[2] < 80
