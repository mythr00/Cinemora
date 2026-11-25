"""Phase 7 / C-13: derived syllable sync is not a user option."""

from services import production_config, templates


def test_config_syllable_accepted_and_demoted_to_word():
    config = production_config.clean_config(
        {"captions": {"mode": "burn", "sync": "syllable"}})
    assert config["captions"]["sync"] == "word"
    flat = production_config.flat_options(config)
    assert flat["caption_sync"] == "word"


def test_config_user_syncs_exclude_syllable():
    assert "syllable" not in production_config.CAPTION_SYNCS
    catalogue = production_config.catalogue()
    assert "syllable" not in catalogue["caption_syncs"]
    assert catalogue["caption_syncs"] == ["word", "off"]


def test_config_word_and_off_unchanged():
    for sync in ("word", "off"):
        config = production_config.clean_config(
            {"captions": {"mode": "burn", "sync": sync}})
        assert config["captions"]["sync"] == sync


def test_template_syllable_accepted_and_demoted():
    validator = templates.OPTION_VALIDATORS["caption_sync"]
    assert validator("caption_sync", "syllable") == "word"
    assert validator("caption_sync", "word") == "word"
    assert validator("caption_sync", "off") == "off"
    assert "syllable" not in templates._CAPTION_SYNC_MODES


def test_internal_renderer_syllable_mechanism_retained(tmp_path):
    # The internal derived mechanism stays (admin diagnostics stamp).
    from services.caption_renderer import create_ass_file
    import json
    captions = tmp_path / "captions.json"
    captions.write_text(json.dumps({
        "segments": [{
            "start": 0.0, "end": 2.0, "text": "Documentary evidence",
            "words": [
                {"word": "Documentary", "start": 0.0, "end": 1.0},
                {"word": "evidence", "start": 1.0, "end": 2.0},
            ],
        }],
    }), encoding="utf-8")
    ass = tmp_path / "out.ass"
    create_ass_file(str(captions), str(ass), sync="syllable")
    assert "; SyllableTiming: derived" in ass.read_text(encoding="utf-8")
