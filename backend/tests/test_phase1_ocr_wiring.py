"""Transformation Phase 1.1 (OCR wiring) — RapidOCR in the
frame-verification provider chain.

Phase 1 built the geography sub-check behind an OCR seam whose
chain (pytesseract -> tesseract binary) had no engine on this
machine, so frame geography recorded ``not_evaluated`` /
``ocr_engine_unavailable`` in production. RapidOCR
(rapidocr-onnxruntime) is installed in the project venv — the
founder delegated the OCR choice (2026-11-22) — and is now the
chain's primary provider: lazy-loaded singleton, fail-soft,
bounded to the already-sampled frames downscaled to
``OCR_MAX_WIDTH``. The producing engine is recorded on the
frame-check record, in the geography sub-record, and in the
asset report's ``visual_frame`` block.

Provider-selection behaviour is pinned with stubbed engines;
the real engine is exercised only in the marked tests at the
bottom (skipped cleanly when the package is unavailable), which
keep their total runtime in seconds.
"""

import importlib.util
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from services import frame_verification as fv
from services import media_probe
from services import story_understanding as story
from services.asset_report import build_asset_report

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = (
    BACKEND_DIR / "media" / "31df4571-c8e1-4bba-be20-fa3a48e2c87f"
)
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

RAPIDOCR_AVAILABLE = (
    importlib.util.find_spec("rapidocr_onnxruntime") is not None
)
requires_rapidocr = pytest.mark.skipif(
    not RAPIDOCR_AVAILABLE, reason="rapidocr-onnxruntime not installed"
)


def _bible():
    return story.hydrate_bible({
        "entities": [
            {"id": "gabby", "canonical": "Gabby Petito",
             "aliases": ["Gabby"], "type": "person"},
            {"id": "moab", "canonical": "Moab, Utah",
             "aliases": [], "type": "place"},
            {"id": "florida", "canonical": "Florida",
             "aliases": [], "type": "place"},
            {"id": "wyoming", "canonical": "Wyoming",
             "aliases": [], "type": "place"},
        ]
    })


def _scenario(**over):
    sc = {
        "subject_id": None,
        "specificity": "representative",
        "search_terms": ["police van driving away"],
        "visual": "a police van driving away after a traffic stop",
        "place": "Moab, Utah",
        "era": "2021",
    }
    sc.update(over)
    return sc


def _frame(tmp_path, name="frame.png", size=(320, 180)):
    path = tmp_path / name
    Image.new("RGB", size, (110, 120, 130)).save(path)
    return path


def _text_image(tmp_path, name, text, size=(1280, 720), font_size=64):
    if not Path(FONT_PATH).exists():
        pytest.skip("DejaVu font unavailable for synthetic frames")
    img = Image.new("RGB", size, (110, 120, 130))
    draw = ImageDraw.Draw(img)
    draw.text(
        (60, size[1] // 2 - font_size // 2), text,
        fill=(245, 245, 245),
        font=ImageFont.truetype(FONT_PATH, font_size),
    )
    path = tmp_path / name
    img.save(path)
    return path


# ---------------------------------------------------------------------------
# Chain order and fall-through (engines stubbed)
# ---------------------------------------------------------------------------

def test_chain_prefers_rapidocr_and_short_circuits(monkeypatch, tmp_path):
    frame = _frame(tmp_path)
    monkeypatch.setattr(fv, "_rapidocr_text", lambda paths: "HELLO")

    def _boom(paths):
        raise AssertionError("fallback must not run when RapidOCR reads")

    monkeypatch.setattr(fv, "_fallback_ocr_text", _boom)
    assert fv._ocr_chain([frame]) == ("HELLO", fv.OCR_ENGINE_RAPIDOCR)
    # The text-only facade keeps the seam's original contract.
    assert fv._default_ocr_provider([frame]) == "HELLO"


def test_chain_falls_through_when_rapidocr_unavailable(
    monkeypatch, tmp_path
):
    frame = _frame(tmp_path)
    monkeypatch.setattr(fv, "_rapidocr_text", lambda paths: None)
    monkeypatch.setattr(
        fv, "_fallback_ocr_text",
        lambda paths: ("LEGACY TEXT", fv.OCR_ENGINE_TESSERACT),
    )
    assert fv._ocr_chain([frame]) == (
        "LEGACY TEXT", fv.OCR_ENGINE_TESSERACT
    )


def test_chain_all_providers_down_records_no_engine(
    monkeypatch, tmp_path
):
    frame = _frame(tmp_path)
    monkeypatch.setattr(fv, "_rapidocr_text", lambda paths: None)
    monkeypatch.setattr(
        fv, "_fallback_ocr_text", lambda paths: (None, None)
    )
    assert fv._ocr_chain([frame]) == (None, None)
    assert fv._default_ocr_provider([frame]) is None


def test_chain_empty_frame_list_never_calls_a_provider(monkeypatch):
    def _boom(paths):
        raise AssertionError("no frames means no OCR")

    monkeypatch.setattr(fv, "_rapidocr_text", _boom)
    monkeypatch.setattr(fv, "_fallback_ocr_text", _boom)
    assert fv._ocr_chain([]) == (None, None)


# ---------------------------------------------------------------------------
# Lazy singleton: built once, failure cached, import is free
# ---------------------------------------------------------------------------

def _fake_rapidocr_module(monkeypatch, engine_class):
    fake = types.ModuleType("rapidocr_onnxruntime")
    fake.RapidOCR = engine_class
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", fake)
    monkeypatch.setattr(fv, "_RAPIDOCR_ENGINE", None)
    monkeypatch.setattr(fv, "_RAPIDOCR_UNAVAILABLE", False)


def test_rapidocr_engine_is_a_lazy_singleton(monkeypatch):
    builds = []

    class FakeEngine:
        def __init__(self):
            builds.append(1)

    _fake_rapidocr_module(monkeypatch, FakeEngine)
    first = fv._rapidocr_engine()
    second = fv._rapidocr_engine()
    assert first is not None and first is second
    assert len(builds) == 1


def test_rapidocr_engine_failure_is_cached_not_retried(monkeypatch):
    attempts = []

    class Boom:
        def __init__(self):
            attempts.append(1)
            raise RuntimeError("models missing")

    _fake_rapidocr_module(monkeypatch, Boom)
    assert fv._rapidocr_engine() is None
    assert fv._rapidocr_engine() is None
    assert len(attempts) == 1
    assert fv._RAPIDOCR_UNAVAILABLE is True
    # ...and the text provider reports unavailable, never raises.
    assert fv._rapidocr_text([Path("whatever.png")]) is None


def test_rapidocr_text_fail_soft_when_engine_raises(
    monkeypatch, tmp_path
):
    class Raising:
        def __call__(self, arg):
            raise RuntimeError("inference boom")

    monkeypatch.setattr(fv, "_rapidocr_engine", lambda: Raising())
    assert fv._rapidocr_text([_frame(tmp_path)]) is None


# ---------------------------------------------------------------------------
# RapidOCR row adaptation and the width bound (engine stubbed)
# ---------------------------------------------------------------------------

class _RecordingEngine:
    def __init__(self, rows):
        self.rows = rows
        self.seen = []

    def __call__(self, arg):
        self.seen.append(arg)
        return self.rows, [0.1, 0.0, 0.1]


def test_rapidocr_frame_lines_adapt_rows(tmp_path):
    engine = _RecordingEngine([
        [[0, 0], "LINE ONE", 0.99],
        [[0, 0], "LINE TWO", 0.98],
        [[0, 0], "", 0.10],
    ])
    frame = _frame(tmp_path)
    assert fv._rapidocr_frame_lines(engine, frame) == [
        "LINE ONE", "LINE TWO",
    ]
    # A narrow frame is handed over as a path, untouched.
    assert engine.seen == [str(frame)]
    empty = _RecordingEngine(None)
    assert fv._rapidocr_frame_lines(empty, frame) == []


def test_rapidocr_frame_lines_downscale_wide_frames(tmp_path):
    engine = _RecordingEngine([[[0, 0], "TEXT", 0.9]])
    wide = _frame(tmp_path, "wide.png", size=(2400, 1200))
    assert fv._rapidocr_frame_lines(engine, wide) == ["TEXT"]
    (array,) = engine.seen
    assert isinstance(array, np.ndarray)
    assert array.shape[1] == fv.OCR_MAX_WIDTH
    assert array.shape[0] == fv.OCR_MAX_WIDTH // 2


# ---------------------------------------------------------------------------
# Engine recording: frame record, geography sub-record, report
# ---------------------------------------------------------------------------

def test_geography_record_carries_the_engine():
    plain = Image.new("RGB", (1280, 720), (110, 120, 130))
    record = fv.verify_frames(
        [plain], kind="image", scenario=_scenario(), bible=_bible(),
        ocr_text="BRIGHTON PIER ENGLAND SEAFRONT",
        ocr_engine=fv.OCR_ENGINE_RAPIDOCR,
    )
    assert record["outcome"] == "reject"
    assert record["ocr_engine"] == "rapidocr"
    assert record["detectors"]["geography"]["engine"] == "rapidocr"

    record = fv.verify_frames(
        [plain], kind="image", scenario=_scenario(), bible=_bible(),
        ocr_text=None,
    )
    geography = record["detectors"]["geography"]
    assert record["ocr_engine"] is None
    assert geography["engine"] is None
    assert geography["detail"] == "ocr_engine_unavailable"


def test_asset_report_visual_frame_ocr_engine():
    empty = build_asset_report([], genre_pack=None)
    block = empty["verification_dimensions"]["visual_frame"]
    assert block["ocr_engine"] is None

    scenes = [{
        "scene_number": 1,
        "asset_stats": {"candidate_records": [{
            "stage": "frame_verification", "candidate_id": "c1",
            "frame_check": {
                "outcome": "pass", "ocr_engine": "rapidocr",
            },
        }]},
        "videos": [{
            "file_path": "/tmp/v.mp4", "type": "video",
            "provider": "youtube", "license": "unknown",
            "verification": {"accepted": True},
            "frame_check": {
                "outcome": "pass", "ocr_engine": "rapidocr",
            },
        }],
        "images": [],
    }]
    block = build_asset_report(
        scenes, genre_pack=None
    )["verification_dimensions"]["visual_frame"]
    assert block["ocr_engine"] == "rapidocr"
    assert block["evaluated_assets"] == 1


# ---------------------------------------------------------------------------
# The real engine (skipped when the package is unavailable)
# ---------------------------------------------------------------------------

@requires_rapidocr
def test_real_engine_reads_synthetic_signage(tmp_path):
    path = _text_image(tmp_path, "sign.png", "MOAB UTAH MAIN STREET")
    text = fv._default_ocr_provider([path])
    assert text is not None
    assert "MOAB" in text.upper()
    assert "UTAH" in text.upper()
    text2, engine = fv._ocr_chain([path])
    assert engine == "rapidocr"
    assert text2 == text


@requires_rapidocr
def test_real_engine_reads_corpus_story_card_frame(tmp_path):
    """The run-9 sentence-17 story card (a different case's viral
    search-party card): its sampled frames carry dense burned-in
    text the real engine must actually read."""
    hits = sorted(PROJECT_DIR.glob("scene_*/sentence_0017_video_*.mp4"))
    if not hits:
        pytest.skip("run-9 story-card video not in corpus")
    probe = media_probe.probe_video(hits[0])
    if probe is None:
        pytest.skip("story-card video unprobeable")
    frames = media_probe.extract_sample_frames(
        hits[0], tmp_path,
        duration_seconds=probe["duration_seconds"],
    )
    if not frames:
        pytest.skip("no frames sampleable from story-card video")
    text = fv._default_ocr_provider(frames)
    assert text
    squashed = "".join(text.split()).upper()
    assert "SEARCHPARTY" in squashed
    assert "RESCUETEAMS" in squashed


@requires_rapidocr
def test_real_engine_geography_rejection_end_to_end(tmp_path):
    """A frame whose OCR text names a country disjoint from the
    scenario's country is rejected contradictory_geography
    through the real provider path — no injected provider."""
    path = _text_image(
        tmp_path, "brighton.png", "BRIGHTON PIER ENGLAND"
    )
    asset = {
        "file_path": str(path), "type": "image",
        "width": 1280, "height": 720,
    }
    record = fv.verify_asset_frames(asset, _scenario(), _bible())
    assert record["outcome"] == "reject"
    assert record["reason"] == story.CONTRADICTORY_GEOGRAPHY
    assert record["ocr_engine"] == "rapidocr"
    geography = record["detectors"]["geography"]
    assert geography["outcome"] == "fail"
    assert geography["engine"] == "rapidocr"
    assert geography["frame_countries"] == ["united kingdom"]
    assert geography["home_countries"] == ["united states"]
