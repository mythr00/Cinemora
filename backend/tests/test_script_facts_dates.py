"""GitHub Skills Programme (2026-11-25): deterministic ISO date
normalisation in the fact table, and dateparser's bounded role
(source stated-date normalisation in source_extraction).

Pins:
- pattern-matched mentions normalise from their own components
  (both word orders, month-year, season-year, bare year);
- a mention with no recorded year NEVER gains an ISO date;
- dated-event facts and their timeline events carry iso_date +
  date_basis through;
- timeline merging treats equal ISO dates as the same date even
  when neither display string contains the other;
- dateparser normalises wild source-date strings and leaves
  unparseable ones unparsed (None), never inventing a date.
"""

from services import script_facts
from services import source_extraction


def _dated_event(text):
    facts = script_facts.extract_script_facts([text])
    events = [f for f in facts if f["kind"] == "dated_event"]
    assert len(events) == 1
    return events[0]


def test_day_month_year_normalises():
    fact = _dated_event("The Berlin Wall fell on 9 November 1989.")
    assert fact["iso_date"] == "1989-11-09"
    assert fact["date_basis"] == "pattern"
    assert fact["precision"] == "day_month_year"


def test_month_day_year_normalises_to_the_same_date():
    fact = _dated_event("On November 9, 1989, the checkpoints opened.")
    assert fact["iso_date"] == "1989-11-09"


def test_month_year_normalises_to_month_precision():
    fact = _dated_event("In November 1989 the trial of the guards began.")
    assert fact["iso_date"] == "1989-11"
    assert fact["precision"] == "month_year"


def test_season_year_normalises_to_the_year_only():
    fact = _dated_event("In the spring of 1961, work on the barrier began.")
    assert fact["iso_date"] == "1961"
    assert fact["precision"] == "season_year"


def test_no_year_mention_never_gains_an_iso_date():
    fact = _dated_event("On 9 November the crowd gathered at the gate.")
    assert fact["year"] is None
    assert fact["iso_date"] is None
    assert fact["date_basis"] is None


def test_bare_year_date_fact_carries_iso():
    facts = script_facts.extract_script_facts(
        ["The treaty was signed in 1961."])
    dates = [f for f in facts if f["kind"] == "date"]
    assert len(dates) == 1
    assert dates[0]["iso_date"] == "1961"
    assert dates[0]["date_basis"] == "pattern"


def test_timeline_events_carry_iso_through():
    table = script_facts.build_fact_table(
        ["The Berlin Wall fell on 9 November 1989."])
    events = script_facts.timeline_events_from_facts(table["facts"])
    assert len(events) == 1
    assert events[0]["iso_date"] == "1989-11-09"
    assert events[0]["date_basis"] == "pattern"
    assert events[0]["when"] == "9 November 1989"


def test_merge_matches_on_equal_iso_despite_display_mismatch():
    existing = [{
        "when": "Nov 9, 1989",
        "what": "The Berlin Wall fell and crowds crossed freely",
        "iso_date": "1989-11-09",
    }]
    facts = script_facts.extract_script_facts(
        ["The Berlin Wall fell on 9 November 1989 as crowds crossed."])
    merged = script_facts.merge_timeline_events(existing, facts)
    assert len(merged) == 1  # recognised as the same event


def test_merge_still_appends_a_genuinely_different_date():
    existing = [{
        "when": "9 November 1989",
        "what": "The Berlin Wall fell and crowds crossed freely",
        "iso_date": "1989-11-09",
    }]
    facts = script_facts.extract_script_facts(
        ["The Berlin Wall was erected on 13 August 1961 as crowds watched."])
    merged = script_facts.merge_timeline_events(existing, facts)
    assert len(merged) == 2


# ---------------------------------------------------------------------------
# dateparser's bounded role: source stated dates
# ---------------------------------------------------------------------------

def test_normalise_date_parses_wild_formats():
    assert source_extraction._normalise_date("9 November 1989") == "1989-11-09"
    assert source_extraction._normalise_date("November 9, 1989") == "1989-11-09"
    assert source_extraction._normalise_date("1989-11-09") == "1989-11-09"


def test_normalise_date_never_invents():
    assert source_extraction._normalise_date("") is None
    assert source_extraction._normalise_date(None) is None
    assert source_extraction._normalise_date("sometime last spring") is None
    assert source_extraction._normalise_date("x" * 200) is None
