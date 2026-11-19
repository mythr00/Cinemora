"""Stage 5A tests: genre packs as data + their wired influences.

- All three packs load from data files with the required fields;
  style families map onto real motion-engine styles (or None).
- Resolution: explicit option > conservative detection > general.
- The detector: Yangtze-style script -> history, Kohberger-style
  script -> true_crime, neutral script -> general, ambiguous
  script stays general.
- Research influence: with a pack, the Stage 3A research pass
  issues the pack's templated queries (stubbed search); without a
  pack the query set is the Stage 3A base set; the pack id is
  recorded in research_meta.
- The asset report records the active pack.
"""

from services import genre_packs
from services.asset_report import build_asset_report
from services.motion_engine.styles import STYLES
from services import story_understanding as story


# ---------------------------------------------------------------------------
# Pack loading
# ---------------------------------------------------------------------------

def test_all_packs_load_with_required_fields():
    for pack_id in ("general", "history", "true_crime"):
        pack = genre_packs.load_pack(pack_id)
        assert pack is not None, pack_id
        assert pack["id"] == pack_id
        for field in (
            "research_query_templates", "entity_handling",
            "format_preferences", "graphics_motifs", "style_family",
            "pacing", "music_mood",
        ):
            assert field in pack, (pack_id, field)
        assert isinstance(pack["graphics_motifs"], list) and pack["graphics_motifs"]
        assert isinstance(pack["entity_handling"], list) and pack["entity_handling"]


def test_pack_style_families_are_real_engine_styles():
    assert genre_packs.load_pack("history")["style_family"] == "history"
    assert genre_packs.load_pack("true_crime")["style_family"] == "true_crime"
    assert genre_packs.load_pack("general")["style_family"] is None
    for pack in genre_packs.available_packs():
        family = pack["style_family"]
        assert family is None or family in STYLES


def test_unknown_pack_id_loads_nothing():
    assert genre_packs.load_pack("western") is None
    assert genre_packs.load_pack("") is None
    assert genre_packs.load_pack(None) is None


def test_true_crime_pack_carries_evidence_discipline():
    notes = " ".join(genre_packs.load_pack("true_crime")["entity_handling"]).lower()
    assert "never invent evidence" in notes
    assert "victim" in notes


# ---------------------------------------------------------------------------
# Resolution + detection
# ---------------------------------------------------------------------------

_HISTORY_SCRIPT = (
    "On 20 April 1949, HMS Amethyst was sailing up the Yangtze when "
    "shore batteries opened fire. The empire's warships had patrolled "
    "the river for a century, and the treaty ports dated back to 1842. "
    "In 1911 the dynasty fell, and by 1937 the expedition upriver had "
    "become part of a wider war."
)

_CRIME_SCRIPT = (
    "The victims were found inside the house, and detectives opened "
    "an investigation. A suspect was arrested and charged with four "
    "counts of murder. Forensic teams searched the crime scene while "
    "the trial was scheduled, and DNA evidence was presented before "
    "he was convicted and sentenced."
)

_NEUTRAL_SCRIPT = (
    "The team released a new version of the software on Tuesday. "
    "Users can now export their projects in several formats, and the "
    "company says a mobile app will follow later this year."
)


def test_detector_history_crime_neutral():
    assert genre_packs.detect_genre(_HISTORY_SCRIPT)["pack_id"] == "history"
    assert genre_packs.detect_genre(_CRIME_SCRIPT)["pack_id"] == "true_crime"
    assert genre_packs.detect_genre(_NEUTRAL_SCRIPT)["pack_id"] == "general"


def test_detector_ambiguous_stays_general():
    # One weak cue from each side — no clear margin, no forced genre.
    text = "The old empire cafe reopened, and a detective novel club meets there on Fridays."
    assert genre_packs.detect_genre(text)["pack_id"] == "general"


def test_resolve_option_wins_over_detection():
    info = genre_packs.resolve_genre_pack({"genre": "true_crime"}, _HISTORY_SCRIPT)
    assert info["pack_id"] == "true_crime"
    assert info["source"] == "option"


def test_resolve_detected_and_default():
    info = genre_packs.resolve_genre_pack(None, _HISTORY_SCRIPT)
    assert info["pack_id"] == "history" and info["source"] == "detected"
    info = genre_packs.resolve_genre_pack({}, _NEUTRAL_SCRIPT)
    assert info["pack_id"] == "general" and info["source"] == "default"


def test_resolve_unknown_option_falls_back_but_is_recorded():
    info = genre_packs.resolve_genre_pack({"genre": "western"}, _NEUTRAL_SCRIPT)
    assert info["pack_id"] == "general"
    assert info["requested"] == "western"


# ---------------------------------------------------------------------------
# Research influence (stubbed search provider)
# ---------------------------------------------------------------------------

def _bible():
    entities = [
        {"id": "amethyst", "canonical": "HMS Amethyst", "type": "thing",
         "aliases": [], "description": "A Royal Navy sloop.", "role": "ship"},
        {"id": "yangtze", "canonical": "Yangtze", "type": "place",
         "aliases": [], "description": "", "role": ""},
    ]
    return {
        "entities": entities,
        "by_id": {e["id"]: e for e in entities},
        "timeline": [],
        "time_period": "1949",
        "primary_subject_id": "amethyst",
    }


def _capture_search(monkeypatch):
    queries = []

    def fake_search(query):
        queries.append(query)
        return []

    monkeypatch.setattr(story, "search_web", fake_search)
    return queries


_SCRIPT = "HMS Amethyst sailed up the Yangtze in 1949."


def test_research_queries_gain_pack_templates(monkeypatch):
    queries = _capture_search(monkeypatch)
    bible = story.enrich_bible_with_research(
        _bible(), _SCRIPT, sentences=[_SCRIPT],
        genre_pack=genre_packs.load_pack("history"),
    )
    assert any("historical record" in q for q in queries), queries
    assert any("primary sources archive" in q for q in queries), queries
    assert any(q == "HMS Amethyst Yangtze history" for q in queries), queries
    assert bible["research_meta"]["genre_pack"] == "history"
    # Bounded: base (<=2) + templates, capped at 5 per subject.
    amethyst_queries = [q for q in queries if q.startswith("HMS Amethyst")]
    assert len(amethyst_queries) <= 5


def test_research_queries_without_pack_are_base_only(monkeypatch):
    queries = _capture_search(monkeypatch)
    bible = story.enrich_bible_with_research(_bible(), _SCRIPT, sentences=[_SCRIPT])
    assert not any("historical record" in q for q in queries), queries
    assert not any("primary sources" in q for q in queries), queries
    assert bible["research_meta"]["genre_pack"] is None


def test_research_queries_true_crime_templates(monkeypatch):
    queries = _capture_search(monkeypatch)
    story.enrich_bible_with_research(
        _bible(), _SCRIPT, sentences=[_SCRIPT],
        genre_pack=genre_packs.load_pack("true_crime"),
    )
    assert any("case timeline" in q for q in queries), queries
    assert any("court records charges" in q for q in queries), queries


# ---------------------------------------------------------------------------
# Asset report recording
# ---------------------------------------------------------------------------

def test_asset_report_records_genre_pack():
    report = build_asset_report([], genre_pack={"id": "history", "source": "option"})
    assert report["genre_pack"] == {"id": "history", "source": "option"}
    report = build_asset_report([])
    assert report["genre_pack"] is None
