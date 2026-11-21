"""Phase 6 — INT-005 cross-test.

A production scene containing BOTH a targeted graphic (Visual
Director manifest identity + regeneration recipe + fact basis on
its timeline item) AND a composed footage item (a validated
lower-third composition plan) goes through the editor:

* revision 0's import carries the manifest id, the recipe and the
  composition plan from the timeline into the editor document;
* a replace_clip on the composed item and a regenerate_graphic on
  the graphic item both succeed, and afterwards the graphic still
  carries its manifest identity + recipe (the regenerated file is
  promoted transactionally) and the replaced item still carries
  its composition plan — manifest, recipe and timeline stay
  consistent across the revision.

Built on the Stage 6 synthetic production (real clips, real
narration, real ledger run), with the Phase 6 stamps applied to
its timeline.json before import — exactly the artefacts a Phase 6
production writes.
"""

import json
import os
from pathlib import Path

import pytest

import stage6_fixture as fx


@pytest.fixture(scope="module")
def production(tmp_path_factory):
    root = tmp_path_factory.mktemp("phase6-media")
    # Save/restore: a bare os.environ write here would leak into
    # later test modules in the same session (the media-root
    # resolution tests run after this file alphabetically).
    _previous_root = os.environ.get("CINEMORA_MEDIA_ROOT")
    os.environ["CINEMORA_MEDIA_ROOT"] = str(root)
    built = fx.build_synthetic_production(root, "proj-phase6-int005")

    from services import composition as composition_module

    timeline_path = built["project_dir"] / "timeline.json"
    timeline = json.loads(timeline_path.read_text())
    items = timeline["scenes"][0]["items"]

    graphic_item = next(
        i for i in items if i.get("assignment") == "motion_graphic"
    )
    graphic_item["manifest_id"] = "gm_0001"
    graphic_item["fact_basis"] = "script_claim"
    graphic_item["theme"] = "archival"
    graphic_item["graphic_recipe"] = {
        "family": "motion_engine",
        "renderer": "motion_engine",
        "template": "data_gauge",
        "template_version": None,
        "inputs": built["graphic_inputs"],
        "style": "history",
        "duration": graphic_item["duration"],
        "purpose": "motion_engine_data_gauge",
        "scene_number": 1,
        "sentence_index": 2,
        "sentence_number": 3,
        "source_references": [
            {"type": "script_sentence", "scene_number": 1,
             "sentence_number": 3},
        ],
        "regenerable": True,
        "non_regenerable_reason": None,
    }

    clip_item = next(
        i for i in items if i["file_path"].endswith("clip_b.mp4")
    )
    layer = composition_module.lower_third_layer(
        "Synthetic Location", None,
        start=0.0, duration=clip_item["duration"], theme="archival",
    )
    plan = {
        "pattern": "lower_third_card",
        "decision_reason": "scene_entry_label",
        "layers": [layer],
    }
    composition_module.validate_composition(plan, clip_item["duration"])
    clip_item["composition"] = plan

    timeline_path.write_text(json.dumps(timeline, indent=2))
    built["root"] = root
    yield built
    if _previous_root is None:
        os.environ.pop("CINEMORA_MEDIA_ROOT", None)
    else:
        os.environ["CINEMORA_MEDIA_ROOT"] = _previous_root


def _rev0(project_id):
    from services import editor

    editor.get_editor(project_id)  # ensures revision 0 exists
    for view in editor.list_revisions(project_id):
        if view["revision_number"] == 0:
            return editor.get_revision(project_id, view["revision_id"])
    raise AssertionError("no revision 0")


def _items(doc, sentence=None):
    items = doc["tracks"]["visual"]
    if sentence is not None:
        items = [i for i in items if i["sentence_number"] == sentence]
    return items


def test_int005_manifest_recipe_and_composition_survive_editor_ops(
    production,
):
    from services import editor

    project_id = production["project_id"]
    _view0, doc0 = _rev0(project_id)

    graphic = next(i for i in _items(doc0, 3) if i["kind"] == "graphic")
    assert graphic["graphic"]["manifest_id"] == "gm_0001"
    assert graphic["graphic"]["fact_basis"] == "script_claim"
    assert graphic["graphic"]["recipe"]["template"] == "data_gauge"

    composed = next(
        i for i in _items(doc0, 2)
        if i["asset"]["file_path"].endswith("clip_b.mp4")
    )
    assert composed["composition"]["pattern"] == "lower_third_card"
    assert (
        composed["composition"]["layers"][0]["validation"]["passed"]
        is True
    )

    _view1, doc1 = editor.apply_operations(
        project_id,
        doc0["revision_id"],
        [
            {"type": "replace_clip", "item_id": composed["item_id"],
             "replacement": {"file_path": str(production["replacement"])}},
            {"type": "regenerate_graphic", "item_id": graphic["item_id"]},
        ],
    )

    graphic1 = next(i for i in _items(doc1, 3) if i["kind"] == "graphic")
    # The regenerated graphic was promoted AND kept its identity:
    # manifest, recipe and fact basis survive the revision.
    assert graphic1["graphic"]["manifest_id"] == "gm_0001"
    assert graphic1["graphic"]["recipe"]["template"] == "data_gauge"
    assert Path(graphic1["asset"]["file_path"]).is_file()
    assert "editor_media" in graphic1["asset"]["file_path"]

    composed1 = next(
        i for i in _items(doc1, 2) if i["kind"] != "gap"
    )
    assert composed1["asset"]["file_path"].endswith("replacement.mp4")
    # The composition plan is a property of the beat's treatment,
    # not of the replaced asset: it survives the swap intact.
    assert composed1["composition"]["pattern"] == "lower_third_card"
    assert (
        composed1["composition"]["layers"][0]["source"]["label"]
        == "Synthetic Location"
    )
