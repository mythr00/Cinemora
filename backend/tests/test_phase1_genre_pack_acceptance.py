"""Transformation Phase 1.2 — genre-pack resolution acceptance.

The run-9 wiring fix (743738c) made a project's stored category
reach genre-pack resolution; runs 10/11 then recorded the
true_crime pack live. This is the suite-level acceptance the
transformation plan (§1.2) asks for: for EACH of the three
existing packs (general, history, true_crime), a project whose
category names the pack must record that pack id with a
non-default source in the asset report — exercised through the
real chain jobs actually run:

    Project.category
      -> jobs._options_with_project_category
      -> genre_packs.resolve_genre_pack
      -> the {"id", "source"} block jobs stamps into
         build_asset_report(genre_pack=...)

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
# history markers, so script detection cannot be what resolves
# the pack — the category must outrank it (resolution order:
# explicit genre > project_category > detection > default).
NEUTRAL_SCRIPT = "A quiet film about bread making and village life."

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


def _make_project(category):
    project_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(Project(
            id=project_id,
            script=NEUTRAL_SCRIPT,
            title=f"phase1 acceptance {category}",
            category=category,
        ))
        db.commit()
    finally:
        db.close()
    return project_id


@pytest.mark.parametrize("pack_id", PACK_IDS)
def test_project_category_records_pack_in_asset_report(
    project_ids, pack_id
):
    project_id = _make_project(pack_id)
    project_ids.append(project_id)

    # The exact chain jobs.run_production_stage / the asset-report
    # call site follow (jobs.py: _options_with_project_category ->
    # resolve_genre_pack -> report block {"id", "source"}).
    merged = jobs._options_with_project_category({}, project_id)
    assert merged.get("project_category") == pack_id

    info = genre_packs.resolve_genre_pack(merged, NEUTRAL_SCRIPT)
    assert info["pack_id"] == pack_id
    assert info["source"] == "project_category"
    assert info["source"] != "default"

    report = build_asset_report(
        [],
        genre_pack={"id": info["pack_id"], "source": info["source"]},
    )
    assert report["genre_pack"] == {
        "id": pack_id,
        "source": "project_category",
    }


def test_unknown_category_does_not_fabricate_a_pack(project_ids):
    """A category naming no known pack must NOT record a pack id
    with a category source — resolution falls through to the
    detector/default honestly (the category string is still the
    project's, it just is not a pack)."""
    project_id = _make_project("Documentary")
    project_ids.append(project_id)
    merged = jobs._options_with_project_category({}, project_id)
    info = genre_packs.resolve_genre_pack(merged, NEUTRAL_SCRIPT)
    assert info["source"] in ("detected", "default")
    if info["source"] == "default":
        assert info["pack_id"] == "general"
