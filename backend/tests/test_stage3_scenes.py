"""Stage 3C: story-structured scenes (services/scene_structure.py,
script_analyzer analysis["scenes"], jobs.build_production_scenes).

Sentence records are modelled on script_analyzer._sentence_record's
real output: text, subject_id, locations, dates, scenarios (each
with subject_id / place / era).
"""

import jobs
from services.scene_structure import derive_scenes


def _sentence(number, text, subject=None, place=None, year=None):
    return {
        "sentence_number": number,
        "text": text,
        "subject_id": subject,
        "locations": [place] if place else [],
        "dates": [str(year)] if year else [],
        "scenarios": [
            {
                "subject_id": subject,
                "place": place,
                "era": str(year) if year else None,
            }
        ]
        if (subject or place or year)
        else [],
        "entities": [],
    }


def _block(start, count, subject, place=None, year=None):
    return [
        _sentence(
            start + i,
            f"Sentence {start + i} about {subject}.",
            subject=subject,
            place=place,
            year=year,
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------
# derive_scenes
# ---------------------------------------------------------------

def test_derive_groups_by_subject_change():
    sentences = _block(1, 3, "alice") + _block(4, 2, "bob")
    scenes = derive_scenes(sentences, {"title": "T"})
    assert [s["sentence_count"] for s in scenes] == [3, 2]
    assert scenes[0]["scene_number"] == 1
    assert scenes[1]["scene_number"] == 2
    assert scenes[0]["bible"] == {"title": "T"}
    assert scenes[0]["sentences"] == sentences[:3]
    assert "Sentence 1" in scenes[0]["text"]


def test_derive_groups_by_place_change():
    sentences = _block(1, 2, "alice", place="Berlin") + _block(
        3, 2, "alice", place="Paris"
    )
    scenes = derive_scenes(sentences)
    assert [s["sentence_count"] for s in scenes] == [2, 2]


def test_derive_groups_by_year_change():
    sentences = _block(1, 2, "alice", year=1961) + _block(
        3, 2, "alice", year=1989
    )
    scenes = derive_scenes(sentences)
    assert [s["sentence_count"] for s in scenes] == [2, 2]


def test_derive_missing_fields_never_force_breaks():
    sentences = [_sentence(i, f"Plain sentence {i}.") for i in range(1, 6)]
    scenes = derive_scenes(sentences)
    assert len(scenes) == 1
    assert scenes[0]["sentence_count"] == 5


def test_derive_subjectless_sentences_continue_scene():
    sentences = (
        _block(1, 2, "alice")
        + [_sentence(3, "He paused for a long moment.")]
        + _block(4, 1, "alice")
    )
    scenes = derive_scenes(sentences)
    assert len(scenes) == 1
    assert scenes[0]["sentence_count"] == 4


def test_derive_caps_scenes_at_40_sentences():
    sentences = _block(1, 45, "alice")
    scenes = derive_scenes(sentences)
    assert [s["sentence_count"] for s in scenes] == [40, 5]


def test_derive_empty_input():
    assert derive_scenes([]) == []
    assert derive_scenes(None) == []


# ---------------------------------------------------------------
# jobs.build_production_scenes
# ---------------------------------------------------------------

def _fake_analysis(sentences, with_scenes=True):
    analysis = {
        "sentences": sentences,
        "bible": {"title": "Fake"},
        "mode": "degraded",
    }
    if with_scenes:
        analysis["scenes"] = derive_scenes(sentences, analysis["bible"])
    return analysis


def _build(monkeypatch, tmp_path, sentences, with_scenes=True):
    analysis = _fake_analysis(sentences, with_scenes=with_scenes)
    monkeypatch.setattr(
        jobs, "analyze_script", lambda *a, **kw: analysis
    )
    script = " ".join(s["text"] for s in sentences)
    return jobs.build_production_scenes(
        script, project_id="proj", project_dir=tmp_path
    )


def test_planned_scenes_used_when_sane(monkeypatch, tmp_path):
    sentences = (
        _block(1, 15, "alice") + _block(16, 15, "bob") + _block(31, 15, "carol")
    )
    built = _build(monkeypatch, tmp_path, sentences)
    scenes = built["scenes"]
    # Story structure: 3 scenes of 15 — flat chunking would give 40+5.
    assert [s["sentence_count"] for s in scenes] == [15, 15, 15]
    assert [s["scene_number"] for s in scenes] == [1, 2, 3]
    flat = [s for scene in scenes for s in scene["sentences"]]
    assert flat == sentences
    assert all(s["project_id"] == "proj" for s in scenes)
    assert all(s["script_hash"] == built["script_hash"] for s in scenes)


def test_fallback_identical_when_analysis_lacks_scenes(monkeypatch, tmp_path):
    sentences = (
        _block(1, 15, "alice") + _block(16, 15, "bob") + _block(31, 15, "carol")
    )
    built = _build(monkeypatch, tmp_path, sentences, with_scenes=False)
    scenes = built["scenes"]
    # Historical behaviour: flat 40-chunks.
    assert [s["sentence_count"] for s in scenes] == [40, 5]
    flat = [s for scene in scenes for s in scene["sentences"]]
    assert flat == sentences


def test_trailing_single_sentence_scene_merges(monkeypatch, tmp_path):
    sentences = _block(1, 3, "alice") + _block(4, 1, "bob")
    built = _build(monkeypatch, tmp_path, sentences)
    scenes = built["scenes"]
    assert len(scenes) == 1
    assert scenes[0]["sentence_count"] == 4


def test_single_planned_scene_of_one_sentence_not_merged(monkeypatch, tmp_path):
    sentences = _block(1, 1, "alice")
    built = _build(monkeypatch, tmp_path, sentences)
    assert len(built["scenes"]) == 1
    assert built["scenes"][0]["sentence_count"] == 1


def test_coverage_sanity_failure_falls_back(monkeypatch, tmp_path):
    sentences = (
        _block(1, 15, "alice") + _block(16, 15, "bob") + _block(31, 15, "carol")
    )
    analysis = _fake_analysis(sentences)
    # Corrupt the plan: drop one sentence from the planned scenes.
    analysis["scenes"][0]["sentences"] = analysis["scenes"][0]["sentences"][:-1]
    analysis["scenes"][0]["sentence_count"] -= 1
    monkeypatch.setattr(jobs, "analyze_script", lambda *a, **kw: analysis)
    script = " ".join(s["text"] for s in sentences)
    built = jobs.build_production_scenes(
        script, project_id="proj", project_dir=tmp_path
    )
    assert [s["sentence_count"] for s in built["scenes"]] == [40, 5]


def test_analyze_script_adds_scenes():
    from services.script_analyzer import analyze_script

    script = (
        "In 1961 the wall rose across Berlin. "
        "Guards watched the border through the night. "
        "The city woke divided."
    )
    analysis = analyze_script(script, project_id="t", llm=None)
    assert "scenes" in analysis
    covered = [
        s for scene in analysis["scenes"] for s in scene["sentences"]
    ]
    assert covered == analysis["sentences"]
    assert all(scene["sentence_count"] >= 1 for scene in analysis["scenes"])
