"""Story-structured scene derivation (Stage 3C, MA-10 / review §2.6).

``derive_scenes`` groups analysed sentence records into scenes by
story structure instead of by a flat sentence count. A new scene
starts when, relative to the scene accumulated so far:

- the sentence's primary subject changes (``subject_id`` on the
  sentence record, falling back to the first scenario's
  ``subject_id``), or
- the scenario place / location changes (the sentence's
  ``locations`` list, falling back to a scenario's ``place``), or
- a year/era in the sentence differs from the running scene's year
  (years are read from the sentence's ``dates``, its scenarios'
  ``era``, and finally its text).

Missing fields never force a break: a sentence with no subject,
place or year simply continues the current scene (and adopts any of
those values it does carry as the scene's running values). That keeps
single-subject scripts to few scenes and never produces one scene per
sentence unless subjects genuinely alternate.

Hard cap: no scene exceeds ``MAX_SCENE_SENTENCES`` (40) sentences —
the historical ``SENTENCES_PER_SCENE`` chunk size — longer runs are
split at the cap.

The returned dicts have EXACTLY the shape ``jobs.build_production_scenes``
produces (scene_number, sentences, text, sentence_count, project_id,
script_hash, bible, case_bible_path); the caller overwrites the
project-specific fields with real values.
"""

from __future__ import annotations

import re

MAX_SCENE_SENTENCES = 40

_YEAR_RE = re.compile(r"\b(1[5-9]\d\d|20\d\d)\b")


def _first_year(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                found = _first_year(item)
                if found is not None:
                    return found
            continue
        match = _YEAR_RE.search(str(value))
        if match:
            return match.group(1)
    return None


def _sentence_subject(sentence):
    subject = sentence.get("subject_id")
    if subject:
        return subject
    for scenario in sentence.get("scenarios") or []:
        if isinstance(scenario, dict) and scenario.get("subject_id"):
            return scenario["subject_id"]
    return None


def _sentence_place(sentence):
    locations = sentence.get("locations") or []
    for location in locations:
        if location:
            return str(location)
    for scenario in sentence.get("scenarios") or []:
        if isinstance(scenario, dict) and scenario.get("place"):
            return str(scenario["place"])
    return None


def _sentence_year(sentence):
    year = _first_year(sentence.get("dates"))
    if year is not None:
        return year
    for scenario in sentence.get("scenarios") or []:
        if isinstance(scenario, dict):
            year = _first_year(scenario.get("era"))
            if year is not None:
                return year
    return _first_year(sentence.get("text"))


def derive_scenes(sentences, bible=None):
    """Group sentence records into story-structured scenes.

    ``sentences`` is the list of records produced by
    ``script_analyzer._sentence_record`` (plain dicts with text and,
    when available, subject/scenario fields). ``bible`` is carried
    onto each scene verbatim, mirroring build_production_scenes.
    """
    bible = bible if isinstance(bible, dict) else {}
    records = [s for s in (sentences or []) if isinstance(s, dict)]
    if not records:
        return []

    groups = []
    current = []
    running_subject = None
    running_place = None
    running_year = None

    def flush():
        nonlocal current, running_subject, running_place, running_year
        if current:
            groups.append(current)
        current = []
        running_subject = None
        running_place = None
        running_year = None

    for sentence in records:
        subject = _sentence_subject(sentence)
        place = _sentence_place(sentence)
        year = _sentence_year(sentence)

        breaks = False
        if current:
            if (
                subject is not None
                and running_subject is not None
                and subject != running_subject
            ):
                breaks = True
            elif (
                place is not None
                and running_place is not None
                and place != running_place
            ):
                breaks = True
            elif (
                year is not None
                and running_year is not None
                and year != running_year
            ):
                breaks = True
            elif len(current) >= MAX_SCENE_SENTENCES:
                breaks = True

        if breaks:
            flush()

        current.append(sentence)
        if subject is not None:
            running_subject = subject
        if place is not None:
            running_place = place
        if year is not None:
            running_year = year

    flush()

    scenes = []
    for index, group in enumerate(groups):
        scenes.append(
            {
                "scene_number": index + 1,
                "sentences": group,
                "text": " ".join(
                    str(s.get("text") or "") for s in group if s.get("text")
                ),
                "sentence_count": len(group),
                "project_id": "",
                "script_hash": "",
                "bible": bible,
                "case_bible_path": "",
            }
        )
    return scenes
