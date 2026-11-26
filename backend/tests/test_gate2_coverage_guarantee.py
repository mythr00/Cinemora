"""Gate 2 (Production Recovery) — workstream 3 tests.

Blocker 2: the graphics (T5) fallback must be a guarantee — the
terminal sweep iterates the registry families whose declared
input requirements the beat's recorded facts satisfy (names,
dates, places, numbers from the sentence, its analysis record,
its scenario and the case bible), and only a beat no family can
render from those facts declines, with the decline recorded
explicitly.

Blocker 3: the pre-render coverage repair pass — after graphics,
sentences still in missing_visuals get ONE bounded repair attempt
through the existing asset machinery; a sentence that survives
repair fails with the gate's own typed coverage error.

Blocker 4: GET /projects/{id}/outputs must report a completed
run-scoped film as existing, with its real path and size.
"""

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_v1
import jobs
import main
from database import engine
from models import Base
from services import graphics_pipeline, media_paths, plan_document


# ---------------------------------------------------------------------
# Shared graphics fixtures (the run-10 seam: stubbed validator,
# fake stat renderer, stubbed terminal templates where a test
# needs the floor itself to refuse).
# ---------------------------------------------------------------------


def _stub_validator(monkeypatch):
    monkeypatch.setattr(
        graphics_pipeline, "validate_graphic_output",
        lambda path, expected_duration=None, **kwargs: {
            "passed": True, "failure_kind": None, "detail": "",
            "checks": {}, "probe": None,
        },
    )
    monkeypatch.setattr(
        graphics_pipeline, "_render_stat_graphic",
        lambda path, *a, **k: Path(path).write_bytes(b"\x00" * 2048),
    )


def _stub_terminal_template(monkeypatch, name, refuse=False):
    from services.motion_engine import templates as engine_templates

    def fake(data, style, out_path, duration=5.0,
             sentence_index=None, scene_number=None):
        if refuse:
            return None, {"reason": "no_text"}
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 4096)
        return str(path), {
            "generator": "motion_engine", "template": name,
            "generated": True,
        }

    monkeypatch.setitem(engine_templates.TEMPLATES, name, fake)


def _manifest(tmp_path):
    return json.loads((tmp_path / "motion_graphics.json").read_text())


# ---------------------------------------------------------------------
# Blocker 2 — terminal graphics guarantee
# ---------------------------------------------------------------------

# A date and a name (and a place) but NO usable statistic:
# extract_stat declines this sentence (verified against the
# extractor — a bare year beside a name is not a count), and
# with an empty scenario the schematic family has no labels.
DATE_NAME_TEXT = (
    "Malcom McLean arrived in Newark in the spring of 1956."
)


def test_terminal_graphic_for_date_and_name_sentence(tmp_path, monkeypatch):
    """(a) A sentence carrying only a date and a name: the stat
    family declines (no usable statistic), the schematic family
    declines (no labels), and the terminal sweep still ends the
    beat with a rendered designed graphic whose payload is the
    short element — never the narration sentence."""
    _stub_validator(monkeypatch)
    _stub_terminal_template(monkeypatch, "closing_card")
    data = {
        "case_bible": {"title": "The Box That Changed Trade",
                       "timeline": [], "entities": []},
        "scenes": [{
            "scene_number": 1,
            "sentences": [{"text": DATE_NAME_TEXT, "sentence_number": 1}],
        }],
    }
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced",
        targets=[{
            "scene_number": 1, "sentence_index": 0, "sentence_number": 1,
            "text": DATE_NAME_TEXT, "scenario": {},
        }],
    )
    assert len(assets) == 1
    card = assets[0]
    assert card["coverage_guarantee"] is True
    assert card["terminal_guarantee"] == "closing_card"
    assert card["delivered"] is True
    payload = card["engine_inputs"]["quote"]
    # The payload is a designed element (the date or the name),
    # never the sentence, never a fragment of it beyond the fact.
    assert payload in ("1956", "Malcom McLean", "MALCOM MCLEAN")
    assert payload != DATE_NAME_TEXT
    assert len(payload.split()) <= 8
    for value in card["engine_inputs"].values():
        if isinstance(value, str):
            assert "cinemora" not in value.lower()
            assert "generated graphic" not in value.lower()
    manifest = _manifest(tmp_path)
    assert manifest["targets"]["generated"] == 1
    assert manifest["targets"]["skipped"] == []
    outcome = manifest["targets"]["outcomes"][0]
    assert outcome["outcome"] == "generated"
    assert outcome["reason"] == "coverage_guarantee:closing_card"


def test_terminal_graphic_from_bible_facts_only(tmp_path, monkeypatch):
    """(a, widened facts) A sentence whose own text yields NO
    selectable payload — its facts live in the case bible. The
    pre-Gate-2 floor attempted an empty card and skipped; the
    sweep builds the payload from the recorded bible facts."""
    _stub_validator(monkeypatch)
    _stub_terminal_template(monkeypatch, "closing_card")
    text = "By then, everything had already changed for her family."
    assert graphics_pipeline.short_payload_for(text) == ""
    data = {
        "case_bible": {
            "title": "Vanished",
            "timeline": [],
            "entities": [
                {"canonical": "Gabby Petito", "type": "person"},
            ],
        },
        "scenes": [{
            "scene_number": 3,
            "sentences": [{"text": text, "sentence_number": 9}],
        }],
    }
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced",
        targets=[{
            "scene_number": 3, "sentence_index": 0, "sentence_number": 9,
            "text": text, "scenario": {},
        }],
    )
    assert len(assets) == 1
    card = assets[0]
    assert card["coverage_guarantee"] is True
    assert card["engine_inputs"]["quote"] == "Gabby Petito"
    assert card["engine_inputs"]["quote"] != text


def test_terminal_decline_is_explicit_when_no_family_can_render(
    tmp_path, monkeypatch
):
    """Only when literally no eligible family can render from the
    recorded facts does the beat decline — and the decline is
    explicit: attempted families + available facts ride both the
    skip entry and the outcome record."""
    _stub_validator(monkeypatch)
    _stub_terminal_template(monkeypatch, "closing_card", refuse=True)
    _stub_terminal_template(monkeypatch, "archival_title", refuse=True)
    data = {
        "case_bible": {"title": "The Box That Changed Trade",
                       "timeline": [], "entities": []},
        "scenes": [{
            "scene_number": 1,
            "sentences": [{"text": DATE_NAME_TEXT, "sentence_number": 1}],
        }],
    }
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced",
        targets=[{
            "scene_number": 1, "sentence_index": 0, "sentence_number": 1,
            "text": DATE_NAME_TEXT, "scenario": {},
        }],
    )
    assert assets == []
    manifest = _manifest(tmp_path)
    assert manifest["targets"]["generated"] == 0
    skips = manifest["targets"]["skipped"]
    assert len(skips) == 1
    assert skips[0]["reason"] in graphics_pipeline.GRAPHIC_SKIP_REASONS
    decline = skips[0]["terminal_decline"]
    assert decline["reason"] == "terminal_graphics_decline"
    assert decline["attempted_families"] == ["closing_card"]
    assert "names" in decline["facts_available"]
    assert "dates" in decline["facts_available"]
    outcome = manifest["targets"]["outcomes"][0]
    assert outcome["outcome"] == "skipped"
    assert outcome["terminal_decline"] == decline


# ---------------------------------------------------------------------
# Blocker 3 — pre-render coverage repair pass
# ---------------------------------------------------------------------


def _processed_scenes(graphics_outcome="skipped"):
    return [{
        "scene": {
            "scene_number": 1,
            "sentences": [
                {"sentence_number": 1, "text": "Covered."},
                {"sentence_number": 2, "text": "The starved beat."},
            ],
        },
        "videos": [],
        "images": [],
        "asset_stats": {
            "missing_visuals": [{
                "sentence_number": 2,
                "reason": "no_verified_asset",
                "graphics_outcome": {
                    "outcome": graphics_outcome,
                    "family": None,
                    "reason": "no_schematic_labels",
                },
            }],
        },
    }]


def test_repair_pass_repairs_still_missing_sentence(tmp_path):
    calls = []

    def repair_fn(scene, project_dir):
        calls.append(scene["scene_number"])
        return {
            "videos": [{"file_path": "/media/repaired.mp4"}],
            "images": [],
            "asset_stats": {"missing_visuals": []},
        }

    processed = _processed_scenes()
    report = jobs._run_coverage_repair(
        processed, tmp_path, repair_fn=repair_fn
    )
    assert calls == [1]
    assert report["attempted"] is True
    assert report["repaired"] == [
        {"scene_number": 1, "sentence_number": 2}
    ]
    assert report["still_missing"] == []
    # The repaired visual merged in; the missing record is gone.
    assert processed[0]["videos"] == [
        {"file_path": "/media/repaired.mp4"}
    ]
    assert processed[0]["asset_stats"]["missing_visuals"] == []


def test_repair_pass_still_uncovered_fails_with_typed_coverage_error(
    tmp_path
):
    """(b) Repair runs, the sentence stays uncovered, and the
    failure is the gate's own typed coverage error — built by the
    same constructor the video_editor gate raises — not a silent
    pass and not a placeholder."""

    def repair_fn(scene, project_dir):
        return {
            "videos": [],
            "images": [],
            "asset_stats": {
                "missing_visuals": [{
                    "sentence_number": 2,
                    "reason": "no_verified_asset",
                }],
            },
        }

    processed = _processed_scenes()
    report = jobs._run_coverage_repair(
        processed, tmp_path, repair_fn=repair_fn
    )
    assert report["attempted"] is True
    assert report["repaired"] == []
    assert report["still_missing"] == [
        {"scene_number": 1, "sentence_number": 2}
    ]
    # The entry survives with the repair stamp — data, not silence.
    entry = processed[0]["asset_stats"]["missing_visuals"][0]
    assert entry["coverage_repair"] == "attempted_still_missing"
    assert entry["graphics_outcome"]["outcome"] == "skipped"

    error = jobs._coverage_incomplete_error(report["still_missing"], 2)
    assert isinstance(error, RuntimeError)
    message = str(error)
    assert message.startswith("TIMELINE COVERAGE INCOMPLETE: ")
    assert "1 of 2 narration sentences have no visual coverage" in message
    assert "sentence 2 (scene 1)" in message
    assert "Refusing to render an incomplete documentary." in message


def test_repair_pass_skips_sentences_graphics_already_served(tmp_path):
    def repair_fn(scene, project_dir):  # pragma: no cover
        raise AssertionError("repair must not run for served beats")

    processed = _processed_scenes(graphics_outcome="generated")
    report = jobs._run_coverage_repair(
        processed, tmp_path, repair_fn=repair_fn
    )
    assert report["attempted"] is False
    assert report["still_missing"] == []


def test_coverage_gate_and_repair_share_the_typed_error():
    """The gate's uncovered entries (with timings) format through
    the same constructor the repair pass uses."""
    error = jobs._coverage_incomplete_error(
        [{
            "sentence_number": 23, "scene_number": 7,
            "start": 93.81, "end": 96.631,
        }],
        23,
    )
    message = str(error)
    assert message.startswith("TIMELINE COVERAGE INCOMPLETE: ")
    assert "sentence 23 (scene 7, 93.81s-96.631s)" in message


# ---------------------------------------------------------------------
# Blocker 4 — outputs surface reports the run-scoped film
# ---------------------------------------------------------------------

TOKEN = "gate2-test-token"
HEADERS = {"X-API-Key": TOKEN}


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    api_v1.rate_limiter.reset()
    # All tables: the outputs path also resolves the calling
    # principal (users) and the retention view, so a subset is
    # not self-sufficient when this module runs alone.
    Base.metadata.create_all(bind=engine)
    yield


def test_outputs_reports_run_scoped_film_as_existing(client):
    """(c) A completed film that exists ONLY in its run directory
    (the DATA-004 layout) reports exists=true with the real path
    and size, and its media key serves the bytes."""
    response = client.post(
        "/api/v1/projects", headers=HEADERS,
        json={"script": f"Script {uuid.uuid4()}. Alpha. Beta."},
    )
    assert response.status_code == 201, response.text
    project_id = response.json()["id"]
    project_dir = media_paths.project_dir(project_id)
    run_dir = project_dir / "runs" / str(uuid.uuid4())
    run_dir.mkdir(parents=True)
    film = run_dir / "documentary_final_output.mp4"
    film_bytes = b"\x00" * 4096
    film.write_bytes(film_bytes)

    outputs = client.get(
        f"/api/v1/projects/{project_id}/outputs", headers=HEADERS
    ).json()
    assert outputs["film"]["exists"] is True
    assert outputs["film"]["size_bytes"] == len(film_bytes)
    assert outputs["film"]["path"] == str(film)
    assert outputs["film"]["media_key"]
    served = client.get(
        f"/api/v1/projects/{project_id}/media/"
        f"{outputs['film']['media_key']}",
        headers=HEADERS,
    )
    assert served.status_code == 200
    assert served.content == film_bytes


def test_outputs_container_film_real_record():
    """(c, real record) The completed Container production whose
    film the audit found misreported: _output_entry against its
    real project directory reports the film as existing."""
    project_id = "545272c8-fd2e-43b5-a227-6839f93317b4"
    project_dir = (
        Path(__file__).resolve().parent.parent
        / "media" / project_id
    )
    film = (
        project_dir / "runs" / "fde5732c-0eb1-403e-a298-2d1752354bc0"
        / "documentary_final_output.mp4"
    )
    if not film.is_file():
        pytest.skip("Container run artefacts not present on this machine")
    index = plan_document.build_media_index(project_id, project_dir)
    entry = api_v1._output_entry(
        project_id, project_dir, index, "documentary_final_output.mp4"
    )
    assert entry["exists"] is True
    assert entry["size_bytes"] == film.stat().st_size
    assert entry["path"] == str(film)
    assert entry["media_key"] in index
