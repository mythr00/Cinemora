"""Phase 9 — Pre-Production Configuration Studio (§§13–17).

The enforcement claims under test (§14.2: configuration is
stored on the project + job and respected by every stage —
never frontend-only):

- Themes: every founder theme id resolves to a real motion
  engine token set; the Grid theme's pattern is actually
  painted by the compositor; a configured theme reaches the
  Visual Director's manifest and the graphics build's recorded
  production theme via the pack override.
- Motion graphics: mode "off" produces zero graphics (the T5
  fallback included); Subtle suppresses the Director's
  composition layer while starved beats are still directed;
  the FS-3 auto intensity derivation -> build parameters
  mapping is exact (see also tests/test_fs_intensity_auto.py).
- Music: placement sections are validated once and gated in
  the ONE mix implementation — a real soundtrack has the bed
  silent outside the selected sections and present inside
  them, and the record states the placement applied.
- Configuration gate: cleaning refuses unknown keys and the
  retired narration_speed by name; pre-generation validation
  refuses a run whose voice would fall back, whose uploaded
  track is missing, or whose library does not exist; a valid
  configuration is stamped into the run's options snapshot
  (the record the Versioning history renders).
- Voice-to-documentary: the transcription interface returns
  the real provider's record for real speech; the API flow
  transcribes, treats (Preserve verbatim; LLM modes are
  proposals, 503 with no provider) and applies — Script Studio
  draft + project script + snapshot.
"""

import subprocess
import uuid
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
from auth_helpers import with_session_token
import jobs
import main
from database import SessionLocal, engine
from models import Base, Plan, ProductionRun, Project, User
from services import auth as auth_service
from services import (
    graphics_pipeline,
    llm_provider,
    music as music_service,
    narration,
    production_config,
    run_ledger,
    transcription,
    visual_director,
)
from services.genre_packs import load_pack
from services.motion_engine import core, themes

TOKEN = "-".join(["phase9", "test", "token"])
PASSWORD = "-".join(["phase9", "test", "password", "123"])

SCRIPT_BODY = (
    "On April 26, 1956, Malcolm McLean loaded 58 containers onto "
    "the Ideal X at Port Newark. The ship sailed for Houston."
)


# ---------------------------------------------------------------------
# Themes: founder vocabulary -> real engine token sets
# ---------------------------------------------------------------------


def test_every_founder_theme_resolves_to_a_real_engine_theme():
    assert sorted(themes.FOUNDER_THEMES) == [
        "archive", "blue", "cinematic", "dark", "documentary",
        "grid", "light", "minimal", "newspaper", "vintage",
    ]
    for founder_id, engine_name in themes.FOUNDER_THEMES.items():
        assert themes.founder_theme_engine_name(founder_id) == engine_name
        tokens = themes.resolve_theme(engine_name)
        assert tokens is not None and tokens["name"] == engine_name
        for key in production_config.TOKEN_KEYS:
            assert isinstance(tokens[key], tuple) and len(tokens[key]) == 3


def test_grid_theme_paints_a_real_grid_in_the_compositor():
    tokens = themes.THEMES["grid_mono"]
    canvas = core.Canvas(
        bg=tokens["bg"],
        pattern=tokens.get("background_pattern"),
        pattern_color=tokens.get("pattern_color"),
    )
    image = canvas.composite()
    # A vertical grid line at x=60, a horizontal at y=60, field
    # black between lines.
    assert image.getpixel((60, 500))[:3] == (64, 64, 64)
    assert image.getpixel((500, 60))[:3] == (64, 64, 64)
    assert image.getpixel((30, 500))[:3] == (0, 0, 0)
    plain = core.Canvas(bg=(0, 0, 0)).composite()
    assert plain.getpixel((60, 500))[:3] == (0, 0, 0)


def test_custom_theme_resolves_to_its_own_token_dict():
    config = production_config.clean_config({
        "theme": {
            "id": "custom", "base": "dark",
            "tokens": {"bg": [1, 2, 3], "accent": [9, 9, 9]},
        }
    })
    resolved = production_config.resolve_theme(config)
    assert resolved["engine_name"] == "custom"
    assert resolved["tokens"]["bg"] == (1, 2, 3)
    assert resolved["tokens"]["accent"] == (9, 9, 9)
    # Untouched tokens come from the base theme (editorial_dark).
    assert resolved["tokens"]["text"] == themes.THEMES["editorial_dark"]["text"]
    # resolve_theme passes the dict through: the dict IS the theme.
    assert themes.resolve_theme(resolved["tokens"]) is resolved["tokens"]


def test_theme_pack_override_reaches_director_and_graphics_build(tmp_path):
    config = production_config.clean_config({"theme": {"id": "blue"}})
    pack, resolved = production_config.theme_pack_override(
        config, load_pack("general")
    )
    assert resolved["engine_name"] == "deep_blue"
    assert pack["visual_theme"] == "deep_blue"
    # The pack's own fields ride untouched.
    assert pack["id"] == "general"

    plan = {
        "version": 1, "project_id": "p9", "analysis_mode": "llm",
        "scene_governor": None,
        "sentences": [{
            "sentence_number": 1, "scene_number": 1, "role": "statement",
            "primary_visual_intent": "exact",
            "secondary_visual_intents": [], "intent_queries": {},
            "graphic_fallback": {"allowed": True, "kind": "stat"},
            "preferred_media_type": "video",
            "specificity_requirement": "exact",
            "visual_misrepresentation_risk": "low",
            "intents_source": "llm_plan", "fallback_concepts": None,
            "concepts_source": None, "entry_tier": "direct",
        }],
    }
    outcomes = {1: {
        "sentence_number": 1, "scene_number": 1, "sentence_index": 0,
        "text": "Exports rose 42 percent.", "scenario": {},
        "covered": False, "starved": True, "tier": None,
        "asset_title": None, "asset_file": None, "scene_photo_count": 0,
    }}
    _plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=pack
    )
    assert manifest["theme"] == "deep_blue"
    assert manifest["entries"][0]["theme"] == "deep_blue"

    data = {"case_bible": {"timeline": []}, "scenes": [],
            "processed_scenes": []}
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", genre_pack=pack
    )
    assert assets == []
    import json

    record = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert record["targeting"]["theme"] == "deep_blue"


# ---------------------------------------------------------------------
# Motion graphics: off means none; subtle drops compositions only
# ---------------------------------------------------------------------


def test_graphics_build_mode_off_produces_nothing(tmp_path):
    data = {"case_bible": {"timeline": []}, "scenes": [],
            "processed_scenes": []}
    targets = [{
        "scene_number": 1, "sentence_index": 0, "sentence_number": 1,
        "text": "Exports rose 42 percent in 1956.",
    }]
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="off", targets=targets
    )
    assert assets == []
    assert list(tmp_path.glob("*.mp4")) == []


def test_graphics_build_params_mapping_is_exact():
    # FS-3: intensity is backend-auto. The toggle still maps to
    # "off" verbatim; the enabled bundles are DERIVED from the
    # production context, and a user-supplied intensity in the
    # submitted document is inert (cleaning normalises it to
    # the "auto" marker, so it cannot steer the result).
    off = production_config.clean_config(
        {"motion_graphics": {"enabled": False}}
    )
    assert production_config.graphics_build_params(off) == {
        "enabled": False, "mode": "off", "max_stats": 0,
        "compositions": False,
        "intensity": None, "intensity_source": "auto",
    }
    config = production_config.clean_config(
        {"motion_graphics": {"enabled": True, "intensity": "dynamic"}}
    )
    assert config["motion_graphics"]["intensity"] == "auto"
    # No production context: the standard (moderate) bundle.
    assert production_config.graphics_build_params(config) == {
        "enabled": True, "mode": "balanced", "max_stats": None,
        "compositions": True,
        "intensity": "moderate", "intensity_source": "auto",
    }
    # No beat needs a graphic: the subtle bundle.
    assert production_config.graphics_build_params(
        config,
        {"graphic_target_count": 0, "sentence_count": 20,
         "timeline_event_count": 2},
    ) == {
        "enabled": True, "mode": "balanced", "max_stats": 1,
        "compositions": False,
        "intensity": "subtle", "intensity_source": "auto",
    }
    # Graphics carry a structural share of the film: dynamic.
    assert production_config.graphics_build_params(
        config,
        {"graphic_target_count": 6, "sentence_count": 20,
         "timeline_event_count": 2},
    ) == {
        "enabled": True, "mode": "heavy", "max_stats": None,
        "compositions": True,
        "intensity": "dynamic", "intensity_source": "auto",
    }


def _director_inputs():
    pack = load_pack("general")
    rows = [
        {
            "sentence_number": 1, "scene_number": 1, "role": "statement",
            "primary_visual_intent": "exact",
            "secondary_visual_intents": [], "intent_queries": {},
            "graphic_fallback": {"allowed": True, "kind": "stat"},
            "preferred_media_type": "video",
            "specificity_requirement": "exact",
            "visual_misrepresentation_risk": "low",
            "intents_source": "llm_plan", "fallback_concepts": None,
            "concepts_source": None, "entry_tier": "direct",
        },
        {
            "sentence_number": 2, "scene_number": 1, "role": "statement",
            "primary_visual_intent": "exact",
            "secondary_visual_intents": [], "intent_queries": {},
            "graphic_fallback": {"allowed": True, "kind": "stat"},
            "preferred_media_type": "video",
            "specificity_requirement": "exact",
            "visual_misrepresentation_risk": "low",
            "intents_source": "llm_plan", "fallback_concepts": None,
            "concepts_source": None, "entry_tier": "direct",
        },
    ]
    plan = {
        "version": 1, "project_id": "p9", "analysis_mode": "llm",
        "scene_governor": None, "sentences": rows,
    }
    outcomes = {
        1: {
            "sentence_number": 1, "scene_number": 1, "sentence_index": 0,
            "text": "", "scenario": {}, "covered": True, "starved": False,
            "tier": "direct", "asset_title": "Ocean Parkway, Long Island",
            "asset_file": "/media/asset.mp4", "scene_photo_count": 0,
        },
        2: {
            "sentence_number": 2, "scene_number": 1, "sentence_index": 1,
            "text": "Exports rose 42 percent.", "scenario": {},
            "covered": False, "starved": True, "tier": None,
            "asset_title": None, "asset_file": None, "scene_photo_count": 0,
        },
    }
    return pack, plan, outcomes


def test_subtle_suppresses_compositions_but_keeps_coverage_graphics():
    pack, plan, outcomes = _director_inputs()
    plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=pack, compositions=True
    )
    first = plan_v2["sentences"][0]["decision"]
    assert first["composition"]["pattern"] == "lower_third_card"
    assert plan_v2["summary"]["compositions"] == 1
    assert plan_v2["summary"]["compositions_enabled"] is True
    assert len(manifest["entries"]) == 1

    plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=pack, compositions=False
    )
    for row in plan_v2["sentences"]:
        assert "composition" not in row["decision"]
    assert plan_v2["summary"]["compositions"] == 0
    assert plan_v2["summary"]["compositions_enabled"] is False
    # The starved beat is still directed to a graphic: subtle
    # removes decoration, not the coverage floor.
    assert len(manifest["entries"]) == 1


def test_disabled_stamp_for_missing_visual_records():
    scenes = [{
        "scene": {"scene_number": 1},
        "asset_stats": {"missing_visuals": [
            {"sentence_number": 3}, {"sentence_number": 4},
        ]},
    }]
    stamped = jobs._annotate_missing_visuals_motion_graphics_disabled(scenes)
    assert stamped == 2
    for entry in scenes[0]["asset_stats"]["missing_visuals"]:
        assert entry["graphics_outcome"] == {
            "outcome": "not_attempted", "family": None,
            "reason": "motion_graphics_disabled",
        }


# ---------------------------------------------------------------------
# Music: sections validated once, gated in the real mix
# ---------------------------------------------------------------------


def test_music_sections_resolution_and_validation():
    decision = music_service.resolve_music_request(
        {"music_placement": "sections",
         "music_sections": [{"start": 2, "end": 4}]},
        Path("/nonexistent-project"),
    )
    assert decision["placement"] == "sections"
    assert decision["sections"] == [{"start": 2.0, "end": 4.0}]
    assert music_service.resolve_music_request({}, Path("/x"))["placement"] \
        == "throughout"
    for bad in (
        {"music_placement": "sections", "music_sections": []},
        {"music_placement": "sections",
         "music_sections": [{"start": 5, "end": 5}]},
        {"music_placement": "sections",
         "music_sections": [{"start": 1, "end": 6}, {"start": 4, "end": 8}]},
        {"music_placement": "middle"},
    ):
        with pytest.raises(ValueError):
            music_service.resolve_music_request(bad, Path("/x"))
    config = music_service.resolve_mix_config(options={
        "music_placement": "sections",
        "music_sections": [{"start": 1, "end": 2}],
    })
    assert config["music_placement"] == "sections"
    assert config["music_sections"] == [{"start": 1.0, "end": 2.0}]


def _tone(path, *, seconds=3.0, freq=None):
    source = (
        f"sine=frequency={freq}:duration={seconds}"
        if freq else f"anullsrc=r=48000:cl=stereo"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", source,
         "-t", str(seconds), str(path)],
        check=True, capture_output=True,
    )


def _rms_db(path, start, duration=0.9):
    result = subprocess.run(
        ["ffmpeg", "-ss", str(start), "-t", str(duration),
         "-i", str(path), "-af", "astats=metadata=1", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    for line in result.stderr.splitlines():
        if "RMS level dB" in line:
            return float(line.rsplit(":", 1)[1].strip())
    raise AssertionError("no RMS measurement in ffmpeg output")


def test_prepare_soundtrack_gates_music_to_sections(tmp_path):
    narration_file = tmp_path / "narration.wav"
    music_file = tmp_path / "music.wav"
    _tone(narration_file)               # silent narration
    _tone(music_file, freq=440)         # constant bed
    mix_config = music_service.resolve_mix_config(options={
        "music_placement": "sections",
        "music_sections": [{"start": 1, "end": 2}],
        "music_volume": 0.5,
    })
    record = music_service.prepare_soundtrack(
        narration_file=str(narration_file),
        output_file=str(tmp_path / "soundtrack.wav"),
        music_file=str(music_file),
        mix_config=mix_config,
        requested=True,
        decided_by="music_file",
    )
    assert record["state"] == "applied"
    assert record["mix"]["placement"] == "sections"
    assert record["mix"]["sections"] == [{"start": 1.0, "end": 2.0}]
    out = tmp_path / "soundtrack.wav"
    inside = _rms_db(out, 1.1)
    outside = _rms_db(out, 2.2)
    assert inside > -30, f"bed missing inside its section: {inside} dB"
    assert outside < inside - 25, (
        f"bed not gated outside its section: inside {inside} dB, "
        f"outside {outside} dB"
    )


# ---------------------------------------------------------------------
# Cleaning + validation: the gate's refusals
# ---------------------------------------------------------------------


def test_cleaning_refusals_are_specific():
    with pytest.raises(production_config.ConfigError) as caught:
        production_config.clean_config({"bogus": {}})
    assert caught.value.code == "unknown_config_key"
    with pytest.raises(production_config.ConfigError) as caught:
        production_config.clean_config(
            {"narration": {"voice_id": "default", "narration_speed": 0.9}}
        )
    assert caught.value.code == "retired_option"
    with pytest.raises(production_config.ConfigError) as caught:
        production_config.clean_config({"theme": {"id": "plaid"}})
    assert caught.value.code == "unknown_theme"
    with pytest.raises(production_config.ConfigError) as caught:
        production_config.clean_config(
            {"narration": {"source": "uploaded_voice"}}
        )
    assert caught.value.code == "narration_source_unsupported"
    with pytest.raises(production_config.ConfigError) as caught:
        production_config.clean_config(
            {"theme": {"id": "custom", "tokens": {"bg": [1, 2]}}}
        )
    assert caught.value.code == "invalid_config"
    with pytest.raises(production_config.ConfigError) as caught:
        production_config.clean_config(
            {"music": {"mix": {"duck_ratio": 500}}}
        )
    assert caught.value.code == "invalid_config"


def test_presets_are_bundles_of_real_settings():
    for preset_id in ("clean", "cinematic", "vintage", "minimal"):
        config = production_config.preset_config(preset_id)
        assert production_config.matching_preset(config) == preset_id
        # Every preset passes live validation on a stock system
        # (no uploads or library involved by construction).
        assert production_config.validate_config(config)["errors"] == []
    edited = production_config.preset_config("clean")
    edited["theme"] = {"id": "grid"}
    assert production_config.matching_preset(edited) == "custom"


def test_validation_refusals_and_warnings():
    bad_voice = production_config.clean_config(
        {"narration": {"voice_id": "en_US-nonexistent-medium"}}
    )
    codes = [e["code"] for e in
             production_config.validate_config(bad_voice)["errors"]]
    assert codes == ["voice_not_installed"]

    missing_upload = production_config.clean_config(
        {"music": {"enabled": True, "source": "upload"}}
    )
    codes = [e["code"] for e in
             production_config.validate_config(missing_upload)["errors"]]
    assert codes == ["music_upload_missing"]

    no_library = production_config.clean_config(
        {"music": {"enabled": True, "source": "library",
                   "track_id": "anything"}}
    )
    codes = [e["code"] for e in
             production_config.validate_config(no_library)["errors"]]
    assert codes == ["music_library_unavailable"]

    disabled = production_config.clean_config(
        {"motion_graphics": {"enabled": False}}
    )
    validation = production_config.validate_config(disabled)
    assert validation["errors"] == []
    assert [w["code"] for w in validation["warnings"]] == [
        "motion_graphics_disabled_coverage"
    ]


# ---------------------------------------------------------------------
# Transcription: the real provider on real speech
# ---------------------------------------------------------------------


def test_transcription_interface_on_real_speech(tmp_path):
    speech = tmp_path / "speech.wav"
    narration.generate_narration(
        "The container ship sailed from Newark to Houston.",
        str(speech),
    )
    record = transcription.transcribe(str(speech))
    assert record["provider"] == "faster_whisper"
    assert record["language"] == "en"
    assert record["segments"], "no segments transcribed"
    text = record["text"].lower()
    assert "ship" in text and "houston" in text
    # Diarization is honestly absent, never fabricated.
    assert record["speakers"] is None
    assert "not performed" in record["speaker_note"]
    for segment in record["segments"]:
        assert "avg_logprob" in segment and "unclear" in segment


# ---------------------------------------------------------------------
# API: catalogue, project config, defaults, run gate, uploads, voice
# ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")  # E-21: explicit opt-in
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.delenv("CINEMORA_MUSIC_CATALOGUE", raising=False)
    monkeypatch.setattr(llm_provider, "BREWKEG_API_KEY", None)
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(fakeredis.FakeRedis(decode_responses=True))

    def _fake_enqueue(project_id, script, options=None, run_id=None):
        if run_id is not None:
            return "job-released"
        merged = {"entrypoint": "enqueue_full_pipeline"}
        if isinstance(options, dict):
            merged.update(options)
        submission = run_ledger.submit_production(
            project_id, script, options=merged
        )
        return submission["run_id"] if submission else "job-unrecorded"

    monkeypatch.setattr(jobs, "enqueue_full_pipeline", _fake_enqueue)
    db = SessionLocal()
    try:
        if db.query(Plan).filter_by(name="free").one_or_none() is None:
            db.add(
                Plan(
                    id=str(uuid.uuid4()),
                    name="free",
                    max_concurrent_runs=2,
                    max_runs_per_period=100,
                    period="month",
                    priority_tier=0,
                    is_default=True,
                )
            )
            db.commit()
        from datetime import datetime, timezone

        leaked = (
            db.query(ProductionRun)
            .filter(
                ProductionRun.status.in_(
                    ("queued", "running", "awaiting_approval")
                )
            )
            .all()
        )
        for row in leaked:
            row.status = "failed"
            row.error = "test-suite cleanup of a leaked active run"
            row.finished_at = datetime.now(timezone.utc)
        if leaked:
            db.commit()
    finally:
        db.close()
    yield
    auth_service.reset_redis_client()


def _register(client, email):
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return with_session_token(response)


def _bearer(user):
    return {"Authorization": f"Bearer {user['session_token']}"}


@pytest.fixture()
def users(client):
    suffix = uuid.uuid4().hex[:8]
    a = _register(client, f"p9-a-{suffix}@example.com")
    b = _register(client, f"p9-b-{suffix}@example.com")
    # Item 8: run submission reserves real billing, so fixture
    # accounts carry purchased credits — the configuration gate
    # under test is exercised under the enforced billing rule.
    from services.billing import credits as credits_service

    db = SessionLocal()
    try:
        for account in (a, b):
            credits_service.grant(
                db,
                account["user"]["id"],
                "purchased",
                5000,
                entry_type="purchase",
                idempotency_key=f"p9-grant-{account['user']['id']}",
                source="test_fixture",
            )
        db.commit()
    finally:
        db.close()
    return a, b


def _create_project(client, headers, **overrides):
    payload = {"script": SCRIPT_BODY, "category": "Industrial"}
    payload.update(overrides)
    response = client.post("/api/v1/projects", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_catalogue_is_generated_from_the_live_systems(client, users):
    a, _ = users
    response = client.get("/api/v1/config/catalogue", headers=_bearer(a))
    assert response.status_code == 200, response.text
    catalogue = response.json()
    assert [t["id"] for t in catalogue["themes"]] == sorted(
        themes.FOUNDER_THEMES
    )
    grid = next(t for t in catalogue["themes"] if t["id"] == "grid")
    assert grid["engine_name"] == "grid_mono"
    assert grid["background_pattern"] == "grid"
    assert grid["swatch"]["bg"] == [0, 0, 0]
    assert [p["id"] for p in catalogue["presets"]] == [
        "clean", "cinematic", "vintage", "minimal",
    ]
    voice_ids = [v["id"] for v in catalogue["narration"]["voices"]]
    assert "default" in voice_ids
    assert catalogue["music_library"]["configured"] is False
    assert catalogue["narration"]["provider"] == "kokoro"
    # The catalogue note is user-facing copy (founder directive
    # 2026-11-26 §I): it tells the user how to choose a voice and
    # carries no engineering description. The no-speed-setting
    # invariant itself is pinned by the rejection tests (a config
    # carrying narration_speed is refused — see the validation
    # tests in this file).
    note = catalogue["narration"]["note"]
    assert "voice" in note
    assert "adapter" not in note and "speed" not in note
    # Every voice carries its natural-name facts, so the UI can
    # label voices "English (UK) — George (male)" and never has
    # to display an internal voice id (directive §G).
    assert all(
        v["name"] and v["region"] and v["gender"]
        for v in catalogue["narration"]["voices"]
    )


def test_create_with_config_projects_flat_keys_and_stores_document(
    client, users
):
    a, _ = users
    headers = _bearer(a)
    project = _create_project(client, headers, options={
        "production_config": {
            "theme": {"id": "vintage"},
            "motion_graphics": {"enabled": False, "intensity": "subtle"},
            "narration": {"voice_id": "default"},
            "captions": {"mode": "sidecar", "sync": "off"},
            "music": {"enabled": False, "source": "none"},
        }
    })
    options = project["options"]
    assert options["production_config"]["theme"] == {"id": "vintage"}
    assert options["visual_theme"] == "vintage"
    assert options["visual_theme_engine"] == "archival"
    assert options["motion_graphics_enabled"] is False
    assert options["voice_id"] == "default"
    assert options["captions"] == "sidecar"
    assert options["caption_sync"] == "off"
    assert options["music_enabled"] is False

    view = client.get(
        f"/api/v1/projects/{project['id']}/config", headers=headers
    ).json()
    assert view["stored"] is True
    assert view["config"]["theme"] == {"id": "vintage"}
    assert view["validation"]["warnings"][0]["code"] == \
        "motion_graphics_disabled_coverage"


def test_put_config_validation_and_cleaning_refusals(client, users):
    a, _ = users
    headers = _bearer(a)
    project = _create_project(client, headers)

    response = client.put(
        f"/api/v1/projects/{project['id']}/config",
        json={"config": {"narration": {"voice_id": "en_US-nope-medium"}}},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "config_validation_failed"
    assert "voice_not_installed" in response.text or \
        "not in the installed voice catalogue" in response.text

    response = client.put(
        f"/api/v1/projects/{project['id']}/config",
        json={"config": {"narration": {"narration_speed": 1.2}}},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unknown_config_key" or \
        response.json()["error"]["code"] == "retired_option"

    # A preset LABEL without its bundle's fields does not stick:
    # the stored label is corrected to what the fields are.
    response = client.put(
        f"/api/v1/projects/{project['id']}/config",
        json={"config": {"preset": "cinematic"}},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["stored"] is True
    assert view["config"]["preset"] == "custom"
    assert view["preset_match"] == "custom"

    # The bundle itself, as the catalogue serves it, keeps its
    # name.
    catalogue = client.get(
        "/api/v1/config/catalogue", headers=headers
    ).json()
    bundle = next(
        p["config"] for p in catalogue["presets"] if p["id"] == "cinematic"
    )
    response = client.put(
        f"/api/v1/projects/{project['id']}/config",
        json={"config": bundle},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["config"]["preset"] == "cinematic"
    assert view["preset_match"] == "cinematic"
    assert view["flat_options"]["visual_theme_engine"] == "cinematic_smoke"
    # FS-3: the cinematic preset's bundled intensity is inert —
    # the flat record states the backend-auto marker, never the
    # preset's (or any user's) chosen value.
    assert view["flat_options"]["motion_graphics_intensity"] == "auto"


def test_validate_endpoint_reports_without_storing(client, users):
    a, _ = users
    headers = _bearer(a)
    project = _create_project(client, headers)
    response = client.post(
        f"/api/v1/projects/{project['id']}/config/validate",
        json={"config": {
            "music": {"enabled": True, "source": "upload"},
            "motion_graphics": {"enabled": False},
        }},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert [e["code"] for e in report["validation"]["errors"]] == [
        "music_upload_missing"
    ]
    assert [w["code"] for w in report["validation"]["warnings"]] == [
        "motion_graphics_disabled_coverage"
    ]
    view = client.get(
        f"/api/v1/projects/{project['id']}/config", headers=headers
    ).json()
    assert view["stored"] is False


def test_run_submission_stamps_the_resolved_config_snapshot(client, users):
    a, _ = users
    headers = _bearer(a)
    project = _create_project(client, headers, options={
        "production_config": {
            "theme": {"id": "blue"},
            "motion_graphics": {"enabled": False},
            "music": {"enabled": False, "source": "none"},
        }
    })
    response = client.post(
        f"/api/v1/projects/{project['id']}/runs", json={}, headers=headers
    )
    assert response.status_code == 202, response.text
    run = run_ledger.latest_run_for(
        project["id"], run_ledger.script_sha256(SCRIPT_BODY)
    )
    assert run is not None
    options = run["options"]
    assert options["production_config"]["theme"] == {"id": "blue"}
    assert options["visual_theme_engine"] == "deep_blue"
    assert options["motion_graphics_enabled"] is False
    resolved = options["production_config_resolved"]
    assert resolved["theme_engine"] == "deep_blue"
    assert resolved["graphics"]["mode"] == "off"
    assert resolved["warnings"][0]["code"] == \
        "motion_graphics_disabled_coverage"

    # The Versioning history renders this same run record.
    # FS-C (FS-11): the versions read is admin-only, so promote
    # the owner to an admin account for this read.
    db = SessionLocal()
    try:
        row = db.get(User, a["user"]["id"])
        row.role = "admin"
        db.commit()
    finally:
        db.close()
    versions = client.get(
        f"/api/v1/projects/{project['id']}/versions", headers=headers
    )
    assert versions.status_code == 200, versions.text
    run_entries = [
        entry for entry in versions.json()["entries"]
        if entry.get("kind") == "run"
    ]
    assert run_entries, versions.text
    assert run_entries[0]["detail"]["options"]["production_config"][
        "theme"
    ] == {"id": "blue"}


def test_run_submission_refuses_an_unhonourable_config(client, users):
    a, _ = users
    headers = _bearer(a)
    # Creation cleans but does not validate: the project exists
    # with a voice the catalogue does not hold.
    project = _create_project(client, headers, options={
        "production_config": {
            "narration": {"voice_id": "en_US-ghost-medium"},
        }
    })
    response = client.post(
        f"/api/v1/projects/{project['id']}/runs", json={}, headers=headers
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "config_validation_failed"
    assert run_ledger.latest_run_for(
        project["id"], run_ledger.script_sha256(SCRIPT_BODY)
    ) is None


def test_defaults_seed_new_projects(client, users):
    a, _ = users
    headers = _bearer(a)
    response = client.put(
        "/api/v1/config/defaults",
        json={"config": {
            "theme": {"id": "grid"},
            "captions": {"mode": "sidecar", "sync": "word"},
        }},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    project = _create_project(client, headers)
    assert project["options"]["production_config"]["theme"] == {"id": "grid"}
    assert project["options"]["visual_theme_engine"] == "grid_mono"
    assert project["options"]["captions"] == "sidecar"

    response = client.delete("/api/v1/config/defaults", headers=headers)
    assert response.status_code == 200, response.text
    project = _create_project(client, headers)
    assert "production_config" not in (project["options"] or {})


def test_config_routes_are_owner_scoped(client, users):
    a, b = users
    project = _create_project(client, _bearer(a))
    for method, suffix in (
        ("get", "/config"),
        ("put", "/config"),
        ("post", "/config/validate"),
        ("get", "/config/voice/transcript"),
    ):
        response = client.request(
            method,
            f"/api/v1/projects/{project['id']}{suffix}",
            json={"config": {}} if method in ("put", "post") else None,
            headers=_bearer(b),
        )
        assert response.status_code == 404, (method, suffix, response.text)


def test_music_upload_flow_and_removal(client, users, tmp_path):
    a, _ = users
    headers = _bearer(a)
    project = _create_project(client, headers)
    track = tmp_path / "track.wav"
    _tone(track, freq=330, seconds=2.0)

    with open(track, "rb") as handle:
        response = client.post(
            f"/api/v1/projects/{project['id']}/config/uploads",
            data={"kind": "music"},
            files={"file": ("track.wav", handle, "audio/wav")},
            headers=headers,
        )
    assert response.status_code == 201, response.text
    upload = response.json()["upload"]
    assert upload["codec"] is not None
    assert upload["content_sha256"]
    assert Path(upload["file_path"]).is_file()

    view = client.get(
        f"/api/v1/projects/{project['id']}/config", headers=headers
    ).json()
    assert view["config"]["music"]["source"] == "upload"
    assert view["flat_options"]["music_file"] == upload["file_path"]
    assert view["validation"]["errors"] == []

    # An unprobeable file is refused and nothing is recorded.
    junk = tmp_path / "junk.wav"
    junk.write_text("not audio at all")
    with open(junk, "rb") as handle:
        response = client.post(
            f"/api/v1/projects/{project['id']}/config/uploads",
            data={"kind": "music"},
            files={"file": ("junk.wav", handle, "audio/wav")},
            headers=headers,
        )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "upload_unprobeable"

    response = client.delete(
        f"/api/v1/projects/{project['id']}/config/music-upload",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["config"]["music"]["source"] == "none"
    assert "music_file" not in view["flat_options"]
    db = SessionLocal()
    try:
        row = db.get(Project, project["id"])
        assert "music_file" not in (row.options or {})
    finally:
        db.close()


def test_voice_to_documentary_flow(client, users, tmp_path, monkeypatch):
    a, _ = users
    headers = _bearer(a)
    project = _create_project(client, headers)

    recording = tmp_path / "voice.wav"
    _tone(recording, freq=220, seconds=1.0)
    with open(recording, "rb") as handle:
        response = client.post(
            f"/api/v1/projects/{project['id']}/config/uploads",
            data={"kind": "voice"},
            files={"file": ("voice.wav", handle, "audio/wav")},
            headers=headers,
        )
    assert response.status_code == 201, response.text

    # The provider seam is the transcription module; stub its
    # result to the recorded shape (the real provider is proven
    # separately on real speech above).
    from services import transcription as transcription_module

    def _fake_transcribe(path, *, provider=None):
        return {
            "provider": "faster_whisper",
            "audio_file": str(path),
            "language": "en",
            "language_probability": 0.99,
            "duration": 4.2,
            "segments": [{
                "start": 0.0, "end": 4.2,
                "text": "The harbour opened in nineteen fifty six.",
                "avg_logprob": -0.2, "no_speech_prob": 0.01,
                "unclear": False, "words": [],
            }],
            "text": "The harbour opened in nineteen fifty six.",
            "unclear_sections": [],
            "speakers": None,
            "speaker_note": "not performed",
        }

    monkeypatch.setattr(transcription_module, "transcribe", _fake_transcribe)

    response = client.post(
        f"/api/v1/projects/{project['id']}/config/voice/transcribe",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["text"] == \
        "The harbour opened in nineteen fifty six."

    response = client.get(
        f"/api/v1/projects/{project['id']}/config/voice/transcript",
        headers=headers,
    )
    assert response.status_code == 200, response.text

    # Preserve: verbatim, no provider needed.
    response = client.post(
        f"/api/v1/projects/{project['id']}/config/voice/treat",
        json={"mode": "preserve"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "mode": "preserve",
        "text": "The harbour opened in nineteen fifty six.",
        "source": "transcript_verbatim",
    }

    # Clean with no LLM provider configured: an honest 503.
    response = client.post(
        f"/api/v1/projects/{project['id']}/config/voice/treat",
        json={"mode": "clean"},
        headers=headers,
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "script_ai_unavailable"

    # Clean with a provider: a proposal, stored nowhere.
    monkeypatch.setattr(llm_provider, "BREWKEG_API_KEY", "test-key")
    monkeypatch.setattr(
        llm_provider, "complete_text",
        lambda system, user: "The harbour opened in 1956.",
    )
    response = client.post(
        f"/api/v1/projects/{project['id']}/config/voice/treat",
        json={"mode": "clean"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "llm_proposal"

    # Apply: project script set + a Script Studio draft created.
    response = client.post(
        f"/api/v1/projects/{project['id']}/config/voice/apply",
        json={"text": "The harbour opened in 1956.\n\nShips followed."},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    applied = response.json()
    assert applied["script_applied"] is True
    assert applied["draft"]["sections"][0]["body"] == \
        "The harbour opened in 1956."
    view = client.get(
        f"/api/v1/projects/{project['id']}", headers=headers
    ).json()
    assert view["script"] == "The harbour opened in 1956.\n\nShips followed."


# ---------------------------------------------------------------------
# Redesign programme (2026-11-27): voice selection integrity.
# The voice the user picks must be the voice the run narrates
# with — no silent replacement by defaults (F1), no silent
# fallback for unvalidated flat voices (F2), and clearing a
# voice must actually clear it (F3).
# ---------------------------------------------------------------------


def test_flat_voice_is_validated_at_the_run_gate():
    from services.production_config import ConfigError

    # A real installed voice passes untouched.
    resolved = production_config.resolve_run_options(
        None, {"voice_id": "bm_george"}
    )
    assert resolved["voice_id"] == "bm_george"

    # A bogus voice refuses BEFORE queueing/charging instead of
    # silently falling back to the default in narration.
    with pytest.raises(ConfigError) as caught:
        production_config.resolve_run_options(
            None, {"voice_id": "no_such_voice"}
        )
    assert caught.value.code == "voice_not_installed"

    # A French voice on an (implicitly English) production
    # refuses as a language mismatch…
    with pytest.raises(ConfigError) as caught:
        production_config.resolve_run_options(
            None, {"voice_id": "ff_siwis"}
        )
    assert caught.value.code == "voice_language_mismatch"

    # …and passes when the production is French.
    resolved = production_config.resolve_run_options(
        None, {"voice_id": "ff_siwis", "language": "fr"}
    )
    assert resolved["voice_id"] == "ff_siwis"


def test_defaults_do_not_overwrite_an_explicit_flat_voice(client, users):
    a, _ = users
    headers = _bearer(a)
    response = client.put(
        "/api/v1/config/defaults",
        json={"config": {"narration": {"voice_id": "af_heart"}}},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    # No explicit pick: the saved default seeds the project.
    project = _create_project(client, headers)
    assert project["options"]["voice_id"] == "af_heart"

    # An explicit pick at creation wins over the default.
    project = _create_project(
        client, headers, options={"voice_id": "bm_george"}
    )
    assert project["options"]["voice_id"] == "bm_george"


def test_clearing_the_config_voice_clears_the_flat_voice(client, users):
    a, _ = users
    headers = _bearer(a)
    project = _create_project(
        client, headers,
        options={
            "production_config": {
                "narration": {"voice_id": "bm_george"},
                "music": {"enabled": False, "source": "none"},
            }
        },
    )
    assert project["options"]["voice_id"] == "bm_george"

    response = client.put(
        f"/api/v1/projects/{project['id']}/config",
        json={"config": {"narration": {"voice_id": None}}},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    refreshed = client.get(
        f"/api/v1/projects/{project['id']}", headers=headers
    ).json()
    assert "voice_id" not in (refreshed["options"] or {})


def test_chosen_voice_reaches_the_run_options(client, users):
    a, _ = users
    headers = _bearer(a)
    project = _create_project(
        client, headers, options={"voice_id": "bm_george"}
    )
    response = client.post(
        f"/api/v1/projects/{project['id']}/runs", json={}, headers=headers
    )
    assert response.status_code == 202, response.text
    run = run_ledger.latest_run_for(
        project["id"], run_ledger.script_sha256(SCRIPT_BODY)
    )
    assert run is not None
    assert run["options"]["voice_id"] == "bm_george"
