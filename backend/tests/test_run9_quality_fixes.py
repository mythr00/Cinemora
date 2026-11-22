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
