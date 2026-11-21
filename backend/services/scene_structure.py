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

Scene governor (FP-2, Master Directive Phase 2): derivation never
emits a 1-sentence scene when a neighbour can absorb it — a lone
opening sentence (the Gilgo shape) folds into the following scene
instead of becoming a starved scene of its own. After narration is
measured, ``split_scenes_by_narration`` splits scenes whose spoken
length exceeds ``SCENE_SPLIT_THRESHOLD_SECONDS`` at beat boundaries
(subject/place runs). The threshold is a FOUNDER EDITORIAL
PARAMETER — a named, recorded, revisable constant, not a tuned
optimum; it is stamped into the production plan so every film
shows the value it was governed by.

The returned dicts have EXACTLY the shape ``jobs.build_production_scenes``
produces (scene_number, sentences, text, sentence_count, project_id,
script_hash, bible, case_bible_path); the caller overwrites the
project-specific fields with real values.
"""

from __future__ import annotations

import re

MAX_SCENE_SENTENCES = 40

#: FP-2 split threshold: scenes whose measured narration runs
#: longer than this are split at beat boundaries. Founder editorial
#: parameter (Master Directive register, FP-2) — revisable by
#: changing this one constant; stamped into every production plan.
SCENE_SPLIT_THRESHOLD_SECONDS = 20.0

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

    groups = _merge_singleton_groups(groups)

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


def _merge_singleton_groups(groups):
    """FP-2 merge rule: fold every 1-sentence group into a
    neighbour (the previous group when the hard cap allows, else
    the next group), preserving sentence order. A script that is a
    single sentence keeps its single scene. Folding iterates:
    absorbing one singleton can leave the next one exposed
    (alternating subjects produce runs of singletons, which
    collapse into one scene — beats inside still preserve the
    subject structure for search and distribution)."""
    groups = [list(group) for group in groups]
    while len(groups) > 1:
        folded = False
        for index, group in enumerate(groups):
            if len(group) != 1:
                continue
            if (
                index > 0
                and len(groups[index - 1]) + 1 <= MAX_SCENE_SENTENCES
            ):
                groups[index - 1].extend(group)
                del groups[index]
            elif (
                index + 1 < len(groups)
                and len(groups[index + 1]) + 1 <= MAX_SCENE_SENTENCES
            ):
                groups[index + 1] = group + groups[index + 1]
                del groups[index]
            else:
                continue
            folded = True
            break
        if not folded:
            break
    return groups


def _beat_groups(records):
    """Runs of sentences sharing a subject/place — the beat
    boundaries a narration split may cut at. Missing values never
    break a run (the derive_scenes rule)."""
    groups = []
    current = []
    running_subject = None
    running_place = None
    for sentence in records:
        subject = _sentence_subject(sentence)
        place = _sentence_place(sentence)
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
        if breaks:
            groups.append(current)
            current = []
            running_subject = None
            running_place = None
        current.append(sentence)
        if subject is not None:
            running_subject = subject
        if place is not None:
            running_place = place
    if current:
        groups.append(current)
    return groups


def split_scenes_by_narration(
    scenes, durations, threshold_seconds=SCENE_SPLIT_THRESHOLD_SECONDS,
):
    """FP-2 post-narration split: scenes whose MEASURED narration
    exceeds ``threshold_seconds`` are split at beat boundaries into
    the fewest children the boundaries allow (a beat group longer
    than the threshold stays whole — a beat is never cut in two).
    ``durations`` is the per-sentence measured duration list for
    the whole script, aligned with the scenes' sentence order.
    Scenes are renumbered sequentially; split children carry a
    ``narration_split`` record naming the threshold, their parent
    and their own measured length. Fail-soft: a durations list
    that does not cover a scene leaves that scene untouched."""
    if not threshold_seconds or threshold_seconds <= 0:
        return list(scenes or [])
    durations = list(durations or [])
    out = []
    offset = 0
    for scene in scenes or []:
        records = list(scene.get("sentences") or [])
        scene_durations = durations[offset:offset + len(records)]
        offset += len(records)
        if len(scene_durations) != len(records) or not records:
            out.append(scene)
            continue
        total = sum(scene_durations)
        if total <= threshold_seconds:
            out.append(scene)
            continue
        # Beat groups with their measured lengths, in order.
        beats = []
        position = 0
        for group in _beat_groups(records):
            beats.append(
                (group, sum(scene_durations[position:position + len(group)]))
            )
            position += len(group)
        if len(beats) <= 1:
            out.append(scene)
            continue
        children = []
        child_records = []
        child_seconds = 0.0
        for group, seconds in beats:
            if (
                child_records
                and child_seconds + seconds > threshold_seconds
            ):
                children.append((child_records, child_seconds))
                child_records = []
                child_seconds = 0.0
            child_records.extend(group)
            child_seconds += seconds
        if child_records:
            children.append((child_records, child_seconds))
        if len(children) <= 1:
            out.append(scene)
            continue
        for child_records, child_seconds in children:
            child = dict(scene)
            child["sentences"] = child_records
            child["text"] = " ".join(
                str(s.get("text") or "")
                for s in child_records
                if s.get("text")
            )
            child["sentence_count"] = len(child_records)
            child["narration_split"] = {
                "threshold_seconds": threshold_seconds,
                "parent_scene_number": scene.get("scene_number"),
                "narration_seconds": round(child_seconds, 3),
            }
            out.append(child)
    for index, scene in enumerate(out):
        scene["scene_number"] = index + 1
    return out
