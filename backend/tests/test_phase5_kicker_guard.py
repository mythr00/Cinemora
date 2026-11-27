"""Phase 5 (A2) — archival_title kicker fact-guard pins.

archival_title is the sole surviving title treatment and renders
well, but its kicker was taken verbatim from a single recorded
field and never checked: the Coffee film's opening title read
"PRESENT DAY" over a film that begins in the Ethiopian highlands
centuries ago. The guard pinned here:

  * A kicker is produced ONLY from recorded facts — the
    sentence record, its scenario, the target scenario, or the
    case bible — never from the sentence's prose.
  * A candidate that the beat's OTHER recorded temporal facts
    contradict is refused (a present-day kicker over recorded
    1800s facts; a dated kicker outside every recorded period).
  * With no recorded fact supporting a kicker, the title
    renders WITHOUT one; when the request made a kicker
    mandatory, the assembler declines instead of inventing one.
"""

from services import graphics_pipeline

OPENING_1800S = (
    "In 1843, traders in the Ethiopian highlands carried coffee "
    "down to the coast."
)


def _data(sentence_extra=None, bible_extra=None):
    sentence = {"text": OPENING_1800S, "sentence_number": 1}
    sentence.update(sentence_extra or {})
    bible = {"title": "Coffee", "timeline": [], "entities": []}
    bible.update(bible_extra or {})
    return {
        "case_bible": bible,
        "scenes": [{"scene_number": 1, "sentences": [sentence]}],
        "processed_scenes": [],
    }


def _build(tmp_path, data, scenario=None):
    entry = {
        "manifest_id": "a2_0001",
        "target": {"scene_number": 1, "sentence_index": 0,
                   "sentence_number": 1},
        "text": OPENING_1800S,
        "scenario": scenario or {},
        "purpose": "coverage_fallback",
        "template_candidates": ["archival_title"],
        "family_candidates": [],
        "theme": None,
        "fact_basis": "script_claim",
        "evidence_ref": None,
        "verified_data": None,
    }
    manifest = {"version": 1, "project_id": "a2", "theme": None,
                "entries": [entry]}
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=manifest)
    return [a for a in assets if a.get("template") == "archival_title"]


def test_no_recorded_era_means_no_kicker(tmp_path):
    # Narration about the 1800s; NO era/section/place recorded
    # anywhere. The title renders without a kicker — a
    # "PRESENT DAY"-style kicker is never invented from prose.
    titles = _build(tmp_path, _data(
        sentence_extra={"dates": ["1843"]}))
    assert len(titles) == 1
    inputs = titles[0]["engine_inputs"]
    assert inputs["kicker"] == ""
    assert inputs["kicker_source"] is None


def test_present_day_kicker_refused_against_recorded_1800s_facts(tmp_path):
    # The Coffee shape: one field claims "Present Day" while the
    # beat's other recorded facts (sentence dates, bible period)
    # place it in the 1800s. The present-day candidate is
    # refused; the corroborated recorded period serves instead.
    titles = _build(
        tmp_path,
        _data(sentence_extra={"dates": ["1843"]},
              bible_extra={"time_period": "19th century"}),
        scenario={"era": "Present Day"},
    )
    assert len(titles) == 1
    inputs = titles[0]["engine_inputs"]
    assert inputs["kicker"] != "Present Day"
    assert inputs["kicker"] == "19th century"
    assert inputs["kicker_source"] == "case_bible"


def test_recorded_era_becomes_the_kicker(tmp_path):
    titles = _build(
        tmp_path,
        _data(sentence_extra={"dates": ["13 August 1961"]}),
        scenario={"era": "August 1961"},
    )
    assert len(titles) == 1
    inputs = titles[0]["engine_inputs"]
    assert inputs["kicker"] == "August 1961"
    assert inputs["kicker_source"] == "scenario"


def test_sentence_record_era_becomes_the_kicker(tmp_path):
    titles = _build(
        tmp_path,
        _data(sentence_extra={
            "era": "the 1550s", "dates": ["1555"],
        }),
    )
    assert len(titles) == 1
    inputs = titles[0]["engine_inputs"]
    assert inputs["kicker"] == "the 1550s"
    assert inputs["kicker_source"] == "sentence"


def test_bible_period_becomes_the_kicker_when_corroborated(tmp_path):
    titles = _build(
        tmp_path,
        _data(sentence_extra={"dates": ["1961"]},
              bible_extra={"time_period": "1960s"}),
    )
    assert len(titles) == 1
    inputs = titles[0]["engine_inputs"]
    assert inputs["kicker"] == "1960s"
    assert inputs["kicker_source"] == "case_bible"


def test_present_day_kicker_kept_when_the_record_supports_it(tmp_path):
    titles = _build(
        tmp_path,
        _data(sentence_extra={"dates": ["2023"]},
              bible_extra={"time_period": "2020s"}),
        scenario={"era": "Present Day"},
    )
    assert len(titles) == 1
    assert titles[0]["engine_inputs"]["kicker"] == "Present Day"


def test_dated_kicker_outside_every_recorded_period_is_refused(tmp_path):
    # Scenario claims 1961; every other recorded fact places the
    # beat in the 1840s and no other candidate exists -> the
    # title renders without a kicker rather than a wrong one.
    titles = _build(
        tmp_path,
        _data(sentence_extra={"dates": ["1843"]},
              bible_extra={"time_period": "1840s"}),
        scenario={"era": "August 1961"},
    )
    assert len(titles) == 1
    inputs = titles[0]["engine_inputs"]
    assert inputs["kicker"] == "1840s"  # the corroborated record
    assert inputs["kicker_source"] == "case_bible"


def test_mandatory_kicker_with_no_recorded_fact_declines(tmp_path):
    titles = _build(
        tmp_path, _data(),
        scenario={"kicker_required": True},
    )
    assert titles == []


def test_mandatory_kicker_with_a_recorded_fact_renders(tmp_path):
    titles = _build(
        tmp_path,
        _data(sentence_extra={"dates": ["1843"]}),
        scenario={"era": "1843", "kicker_required": True},
    )
    assert len(titles) == 1
    assert titles[0]["engine_inputs"]["kicker"] == "1843"
