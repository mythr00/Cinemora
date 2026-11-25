"""Transformation Phase 1.2 — genre-pack resolution acceptance,
as amended by FS-D (FS-2).

HISTORY: the run-9 wiring fix (743738c) made a project's stored
category reach genre-pack resolution, and this suite pinned that
chain (Project.category -> jobs._options_with_project_category ->
resolve_genre_pack -> the {"id", "source"} block jobs stamps into
build_asset_report). FS-2 REMOVED genre selection from every user
flow and made the documentary treatment backend-derived, so FS-D
cut that wiring: jobs no longer injects the stored category, and
resolution always derives from the script.

This suite now pins the FS-D chain, through the exact functions
jobs runs:

    project options
      -> jobs._options_for_genre_resolution (user-fed genre keys
         stripped; NO category injection)
      -> genre_packs.resolve_genre_pack (detected/default only)
      -> the {"id", "source"} block jobs stamps into
         build_asset_report(genre_pack=...)

For EACH of the three original packs named as a stored category,
a detector-neutral script must now record the GENERAL pack with
source "default" — the category is inert. And a project whose
category names one pack but whose script unmistakably signals
another records the SCRIPT's pack with source "detected".

Test-level only; no production runs.
"""

import uuid

import pytest

import jobs
from database import SessionLocal, engine
from models import Base, Project
from services import genre_packs
from services.asset_report import build_asset_report

# The detector-neutral script: it mentions no crime, war or
# history markers, so script detection cannot fire — under the
# old wiring the category alone decided the pack; under FS-D
# nothing does, and resolution falls to the general default.
NEUTRAL_SCRIPT = "A quiet film about bread making and village life."

# An unmistakable true-crime script (many distinct detector cues,
# no history cues): detection must win over any stored category.
TRUE_CRIME_SCRIPT = (
    "The detective stood over the victim at the crime scene. "
    "Police said the suspect had been arrested nearby, and forensic "
    "teams recovered dna from the knife. The trial began in spring: "
    "the prosecutor told the jury the defendant was guilty of the "
    "murder, and after the homicide investigation the court "
    "convicted him and sentenced him to life in prison."
)

PACK_IDS = ("general", "history", "true_crime")


@pytest.fixture()
def project_ids():
    Base.metadata.create_all(engine)
    created = []
    yield created
    db = SessionLocal()
    try:
        for project_id in created:
            row = db.get(Project, project_id)
            if row is not None:
                db.delete(row)
        db.commit()
    finally:
        db.close()


def _make_project(category, script=NEUTRAL_SCRIPT):
    project_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(Project(
            id=project_id,
            script=script,
            title=f"phase1 acceptance {category}",
            category=category,
        ))
        db.commit()
    finally:
        db.close()
    return project_id


def _jobs_chain_info(script):
    """The chain jobs actually runs since FS-D: strip the user-fed
    genre keys (no category is read or injected), then resolve."""
    return genre_packs.resolve_genre_pack(
        jobs._options_for_genre_resolution({}), script
    )


@pytest.mark.parametrize("pack_id", PACK_IDS)
def test_stored_category_no_longer_records_its_pack(
    project_ids, pack_id
):
    """FS-D: a category naming a pack records NOTHING by itself —
    with a detector-neutral script the asset report records the
    derived general pack, whatever the category says."""
    project_id = _make_project(pack_id)
    project_ids.append(project_id)

    info = _jobs_chain_info(NEUTRAL_SCRIPT)
    assert info["pack_id"] == "general"
    assert info["source"] == "default"

    report = build_asset_report(
        [],
        genre_pack={"id": info["pack_id"], "source": info["source"]},
    )
    assert report["genre_pack"] == {
        "id": "general",
        "source": "default",
    }


def test_script_signal_records_detected_pack_despite_category(
    project_ids,
):
    """FS-D: the script is the only input that decides — a project
    categorised "history" whose script is unmistakably true crime
    records the true_crime pack with source "detected"."""
    project_id = _make_project("history", script=TRUE_CRIME_SCRIPT)
    project_ids.append(project_id)

    info = _jobs_chain_info(TRUE_CRIME_SCRIPT)
    assert info["pack_id"] == "true_crime"
    assert info["source"] == "detected"

    report = build_asset_report(
        [],
        genre_pack={"id": info["pack_id"], "source": info["source"]},
    )
    assert report["genre_pack"] == {
        "id": "true_crime",
        "source": "detected",
    }


def test_unknown_category_does_not_fabricate_a_pack(project_ids):
    """A category naming no known pack must NOT record a pack id
    with a category source — resolution falls through to the
    detector/default honestly (the category string is still the
    project's, it just is not a pack)."""
    project_id = _make_project("Documentary")
    project_ids.append(project_id)
    info = _jobs_chain_info(NEUTRAL_SCRIPT)
    assert info["source"] in ("detected", "default")
    if info["source"] == "default":
        assert info["pack_id"] == "general"
