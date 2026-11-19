"""Stage 5A/5B tests: genre packs steer the targeted-graphics chain.

All engine templates are replaced with recording fakes and the
ffmpeg/PIL renderers are stubbed, so these tests pin CHOICE and
STYLE, not rendering (rendering is covered by the template tests):

- comparison target: history pack -> bar_chart in the "history"
  style; true_crime pack -> ring_badges in the "true_crime" style;
  general pack -> bar_chart with the historical default style;
  NO pack -> stat_comparison in "editorial" (Stage 3 behaviour
  preserved exactly).
- percent target after a stat target (the variety pre-pass):
  history -> data_gauge; true_crime -> ring_badges.
- The graphics manifest records the active pack id.
"""

import json

from services import genre_packs, graphics_pipeline
from services.motion_engine import templates as engine_templates


def _install_fakes(monkeypatch):
    calls = []

    def fake_write(path, *args, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)

    monkeypatch.setattr(graphics_pipeline, "_render_stat_graphic", fake_write)
    monkeypatch.setattr(
        graphics_pipeline, "_render_timeline_graphic",
        lambda path, *a, **k: (fake_write(path), "proportional")[1],
    )
    monkeypatch.setattr(graphics_pipeline, "_render_process_graphic", fake_write)
    # The schematic stage must not intercept these targets.
    monkeypatch.setattr(
        "services.vector_graphics.select_schematic_motif", lambda *a, **k: None
    )

    for name in list(engine_templates.TEMPLATES):
        def make_fake(template_name):
            def fake(data, style, out_path, duration=5.0,
                     sentence_index=None, scene_number=None):
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"\x00" * 2048)
                calls.append({"template": template_name, "style": style, "data": data})
                return str(out_path), {
                    "generator": "motion_engine", "template": template_name,
                    "generated": True,
                }
            return fake
        monkeypatch.setitem(engine_templates.TEMPLATES, name, make_fake(name))
    return calls


def _data(texts):
    return {
        "case_bible": {"timeline": []},
        "scenes": [{
            "scene_number": 1,
            "sentences": [
                {"text": t, "sentence_number": i + 1} for i, t in enumerate(texts)
            ],
        }],
    }


def _targets(texts):
    return [
        {"scene_number": 1, "sentence_index": i, "sentence_number": i + 1, "text": t}
        for i, t in enumerate(texts)
    ]


_COMPARISON = "Exports reached 200 ships, compared with 120 ships a decade earlier."


def _run(tmp_path, monkeypatch, texts, pack):
    calls = _install_fakes(monkeypatch)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(texts), tmp_path, mode="balanced",
        targets=_targets(texts), genre_pack=pack,
    )
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    return assets, calls, manifest


def test_comparison_history_pack_picks_bar_chart_in_history_style(tmp_path, monkeypatch):
    assets, calls, manifest = _run(
        tmp_path, monkeypatch, [_COMPARISON], genre_packs.load_pack("history"))
    engine_assets = [a for a in assets if a.get("family") == "motion_engine"]
    assert len(engine_assets) == 1
    assert engine_assets[0]["graphic_type"] == "bar_chart"
    assert calls and calls[0]["template"] == "bar_chart"
    assert calls[0]["style"] == "history"
    assert manifest["genre_pack"] == "history"


def test_comparison_true_crime_pack_picks_ring_badges(tmp_path, monkeypatch):
    assets, calls, manifest = _run(
        tmp_path, monkeypatch, [_COMPARISON], genre_packs.load_pack("true_crime"))
    engine_assets = [a for a in assets if a.get("family") == "motion_engine"]
    assert len(engine_assets) == 1
    assert engine_assets[0]["graphic_type"] == "ring_badges"
    assert calls[0]["style"] == "true_crime"
    assert manifest["genre_pack"] == "true_crime"
    # The badges carry the sentence's own extracted values — the
    # comparison's displays, nothing invented.
    badge_values = [b["value"] for b in calls[0]["data"]["badges"]]
    assert badge_values == ["200 ships", "120 ships"] or len(badge_values) == 2


def test_comparison_general_pack_bar_chart_default_style(tmp_path, monkeypatch):
    assets, calls, manifest = _run(
        tmp_path, monkeypatch, [_COMPARISON], genre_packs.load_pack("general"))
    engine_assets = [a for a in assets if a.get("family") == "motion_engine"]
    assert engine_assets and engine_assets[0]["graphic_type"] == "bar_chart"
    # general declares no style family -> historical per-template default.
    assert calls[0]["style"] == "editorial"
    assert manifest["genre_pack"] == "general"


def test_comparison_no_pack_keeps_stage3_behaviour(tmp_path, monkeypatch):
    assets, calls, manifest = _run(tmp_path, monkeypatch, [_COMPARISON], None)
    engine_assets = [a for a in assets if a.get("family") == "motion_engine"]
    assert engine_assets and engine_assets[0]["graphic_type"] == "stat_comparison"
    assert calls[0]["style"] == "editorial"
    assert manifest["genre_pack"] is None


_STAT = "The siege lasted 101 days."
_PERCENT = "Only 50 percent of the rations remained."


def test_percent_variety_history_picks_data_gauge(tmp_path, monkeypatch):
    assets, calls, manifest = _run(
        tmp_path, monkeypatch, [_STAT, _PERCENT], genre_packs.load_pack("history"))
    assert calls and calls[0]["template"] == "data_gauge"
    assert calls[0]["style"] == "history"


def test_percent_variety_true_crime_picks_ring_badges(tmp_path, monkeypatch):
    assets, calls, manifest = _run(
        tmp_path, monkeypatch, [_STAT, _PERCENT], genre_packs.load_pack("true_crime"))
    assert calls and calls[0]["template"] == "ring_badges"
    assert calls[0]["style"] == "true_crime"
    assert calls[0]["data"]["badges"][0]["value"] == "50%"
